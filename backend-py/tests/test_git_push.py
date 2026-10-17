"""git_push 工具与推送编排的测试。

重点保护三条设计约束：
1. **凭据不进沙箱**：Agent 侧只调用后端编排，沙箱里没有 token（所以 push 不能走 `git` 工具）。
2. **前置条件缺失不可重试**：缺仓库地址 / 缺凭据 / 无沙箱 / 无分支 → retryable=False，
   让模型直接告诉用户去配置，而不是反复重试烧 token。
3. **一份实现两个入口**：HTTP 的 `/internal/git/push` 与 Agent 的 `git_push` 共用
   `WorkspacePush.push`，不在两处各写一遍推送流程。
"""

from __future__ import annotations

import asyncio

import pytest

from edith_backend.internal.gitops import orchestrator
from edith_backend.internal.gitops.orchestrator import PushRejected, WorkspacePush
from edith_backend.internal.sandbox.tools import SandboxToolSet


class _Workspaces:
    def __init__(self, record: dict | None) -> None:
        self._record = record

    async def get(self, user_id: str, workspace_id: str) -> dict | None:
        return self._record


class _Credentials:
    def __init__(self, token: str) -> None:
        self._token = token

    async def load_token(self, user_id: str) -> str:
        return self._token


class _Sandboxes:
    def __init__(self, sandbox: object | None) -> None:
        self._sandbox = sandbox

    async def existing_workspace(self, user_id: str, session_id: str) -> object:
        if self._sandbox is None:
            from edith_backend.internal.sandbox.service import SandboxNotFound

            raise SandboxNotFound("no sandbox")
        return self._sandbox


def _pusher(record: dict | None = None, token: str = "ghp_test", sandbox=None) -> WorkspacePush:
    return WorkspacePush(_Sandboxes(sandbox), _Credentials(token), _Workspaces(record))


def test_push_rejects_when_workspace_missing() -> None:
    with pytest.raises(PushRejected) as exc_info:
        asyncio.run(_pusher(record=None, sandbox=object()).push("u1", "s1", "w1"))
    assert exc_info.value.code == "workspace_not_found"
    assert exc_info.value.retryable is False


def test_push_rejects_without_remote_url() -> None:
    with pytest.raises(PushRejected) as exc_info:
        asyncio.run(
            _pusher(record={"metadata": {}}, sandbox=object()).push("u1", "s1", "w1")
        )
    assert exc_info.value.code == "git_remote_missing"


def test_push_rejects_private_remote() -> None:
    """SSRF 校验：内网地址必须在推送前被拒。"""
    record = {"metadata": {"gitRemoteUrl": "http://10.0.0.5/x.git"}}
    with pytest.raises(PushRejected) as exc_info:
        asyncio.run(_pusher(record=record, sandbox=object()).push("u1", "s1", "w1"))
    assert exc_info.value.code == "invalid_remote"


def test_push_rejects_without_credential() -> None:
    record = {"metadata": {"gitRemoteUrl": "https://github.com/o/r.git"}}
    with pytest.raises(PushRejected) as exc_info:
        asyncio.run(
            _pusher(record=record, token="", sandbox=object()).push("u1", "s1", "w1")
        )
    assert exc_info.value.code == "git_credential_missing"


def test_push_rejects_without_sandbox() -> None:
    record = {"metadata": {"gitRemoteUrl": "https://github.com/o/r.git"}}
    with pytest.raises(PushRejected) as exc_info:
        asyncio.run(_pusher(record=record, sandbox=None).push("u1", "s1", "w1"))
    assert exc_info.value.code == "sandbox_not_found"


def test_push_exports_bundle_and_pushes(monkeypatch) -> None:
    """happy path：沙箱只导出 bundle，push 由后端带凭据完成。"""
    seen: dict = {}

    async def fake_export(sandbox, work_path, branch):
        seen["export"] = (work_path, branch)
        return b"bundle-bytes"

    async def fake_push(bundle, remote_url, branch, token, *, target_branch=""):
        seen["push"] = (bundle, remote_url, branch, token, target_branch)
        return {"branch": branch, "remoteBranch": target_branch or branch,
                "remoteUrl": remote_url}

    monkeypatch.setattr(orchestrator, "export_bundle", fake_export)
    monkeypatch.setattr(orchestrator, "push_bundle", fake_push)

    record = {"metadata": {"gitRemoteUrl": "https://github.com/o/r.git"}}
    result = asyncio.run(
        _pusher(record=record, token="ghp_x", sandbox=object()).push(
            "u1", "s1", "w1", "master", "main"
        )
    )
    assert result == {
        "branch": "master",
        "remoteBranch": "main",
        "remoteUrl": "https://github.com/o/r.git",
        "bundleBytes": len(b"bundle-bytes"),
    }
    assert seen["push"][3] == "ghp_x"  # 凭据只出现在后端这一步
    assert seen["export"][1] == "master"   # 导出的是本地分支
    assert seen["push"][4] == "main"       # 推的是远端分支（可与本地不同名）


def test_tool_definitions_expose_git_push() -> None:
    registry = object.__new__(SandboxToolSet)
    definitions = SandboxToolSet.tool_definitions(registry)
    names = [d["function"]["name"] for d in definitions]
    assert "git_push" in names
    assert "git_remote_branches" in names
    push_def = next(d for d in definitions if d["function"]["name"] == "git_push")
    # 不能接受任意 flag（否则等于绕开 policy 的禁强推约束）
    assert set(push_def["function"]["parameters"]["properties"]) == {
        "branch",
        "remoteBranch",
    }
    # git 工具的描述必须把推送指向 git_push，否则模型会继续尝试 git push 然后被拒
    git_def = next(d for d in definitions if d["function"]["name"] == "git")
    assert "git_push" in git_def["function"]["description"]


def test_op_git_push_requires_workspace() -> None:
    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._push_workspace = None
    result = asyncio.run(
        SandboxToolSet._op_git_push(registry, {}, "u1", "s1", "")
    )
    assert result["kind"] == "command_error"
    assert result["retryable"] is False


def test_op_git_push_maps_business_error_without_retry() -> None:
    async def denied(user_id, session_id, workspace_id, branch, remote_branch):
        raise PushRejected("git_credential_missing", "尚未连接 GitHub")

    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._push_workspace = denied
    result = asyncio.run(
        SandboxToolSet._op_git_push(registry, {}, "u1", "s1", "w1")
    )
    assert result["kind"] == "command_error"
    assert result["code"] == "git_credential_missing"
    assert result["retryable"] is False
    # 缺凭据要给「用户该做什么」，而不是让模型重试
    assert "连接 GitHub" in result["suggestion"]


def test_op_git_push_reraises_unexpected_error() -> None:
    """代码 bug 不能被伪装成「推送失败」诱导模型重试。"""

    async def boom(user_id, session_id, workspace_id, branch, remote_branch):
        raise TypeError("bug")

    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._push_workspace = boom
    with pytest.raises(TypeError):
        asyncio.run(
            SandboxToolSet._op_git_push(registry, {}, "u1", "s1", "w1")
        )


def test_op_git_push_forwards_remote_branch() -> None:
    """本地 master → 远端 main：两个分支名必须分别透传到编排层。"""
    seen: dict = {}

    async def capture(user_id, session_id, workspace_id, branch, remote_branch):
        seen.update(branch=branch, remote_branch=remote_branch)
        return {"branch": branch, "remoteBranch": remote_branch, "remoteUrl": "u"}

    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._push_workspace = capture
    result = asyncio.run(
        SandboxToolSet._op_git_push(
            registry, {"branch": "master", "remoteBranch": "main"}, "u1", "s1", "w1"
        )
    )
    assert seen == {"branch": "master", "remote_branch": "main"}
    assert result["pushed"] is True
    assert result["remoteBranch"] == "main"


def test_op_git_push_non_fast_forward_is_not_retryable() -> None:
    """非快进必须给出「换分支推 / 先合并」的指引，而不是让模型重试。"""

    async def rejected(user_id, session_id, workspace_id, branch, remote_branch):
        raise PushRejected("push_non_fast_forward", "push 被拒（非快进）")

    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._push_workspace = rejected
    result = asyncio.run(
        SandboxToolSet._op_git_push(registry, {}, "u1", "s1", "w1")
    )
    assert result["code"] == "push_non_fast_forward"
    assert result["retryable"] is False
    assert "remoteBranch" in result["suggestion"]


def test_op_git_remote_branches_requires_workspace() -> None:
    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._remote_info = None
    result = asyncio.run(
        SandboxToolSet._op_git_remote_branches(registry, "u1", "")
    )
    assert result["retryable"] is False


def test_op_git_remote_branches_returns_info() -> None:
    async def info(user_id, workspace_id):
        return {"remoteUrl": "https://github.com/o/r.git",
                "defaultBranch": "master",
                "branches": [{"name": "main", "sha": "a"}]}

    registry = SandboxToolSet.__new__(SandboxToolSet)
    registry._remote_info = info
    result = asyncio.run(
        SandboxToolSet._op_git_remote_branches(registry, "u1", "w1")
    )
    assert result["defaultBranch"] == "master"

