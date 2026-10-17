"""工作区推送到远端仓库的**唯一**编排实现（D-2）。

凭据隔离原则：Agent 沙箱里**没有** token，只负责 `git bundle create` 把提交打包；
后端读出 bundle，在自己的临时目录 `git fetch` 后带凭据 push。remote URL 保持干净
（不含 token），不会写进 `.git/config`。

两个入口共用这一份编排，不在各自文件里重写一遍：
- HTTP 入口 `POST /internal/git/push`（前端「推送到 GitHub」按钮）
- Agent 工具 `git_push`（让模型能自己完成提交 + 推送）

注意：`gitops/__init__.py` **不导出本模块**（它 import sandbox.service，
而 sandbox.tools 又 import gitops），避免循环导入。
"""

from __future__ import annotations

import logging

from ..sandbox.service import SandboxNotFound
from ..sandbox.service import Service as SandboxService
from ..userconfig.gitcredentials import GitCredentials
from ..workspace.layout import WORKSPACE
from ..workspace.store import WorkspaceStore
from .bundle import BundleExportError, current_branch, export_bundle
from .push import (
    PushError,
    PushNonFastForward,
    list_remote_branches,
    push_bundle,
)
from .ssrf import CloneUrlRejected, assert_safe_clone_url

logger = logging.getLogger(__name__)

# 工作区 metadata 中存远端地址的键
GIT_REMOTE_KEY = "gitRemoteUrl"

# 错误码 → (HTTP 状态, 是否值得重试)
# 「值得重试」是给 Agent 看的：缺凭据/缺仓库地址这类要用户动手，重试只会白烧一轮。
_ERROR_META = {
    "workspace_not_found": (404, False),
    "git_remote_missing": (400, False),
    "invalid_remote": (400, False),
    "git_credential_missing": (400, False),
    "sandbox_not_found": (404, False),
    "no_branch": (400, False),
    "bundle_export_failed": (400, True),
    # 非快进：重试没有意义（远端不会自己让路），必须换目标或先合并
    "push_non_fast_forward": (409, False),
    "push_failed": (502, True),
    "remote_query_failed": (502, True),
}


class PushRejected(Exception):
    """推送前置条件不满足或推送失败（可读原因，已脱敏）。

    code 是稳定标识（供前端/Agent 判断），status/retryable 由 code 查表得出，
    避免各调用方各写一套映射。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.status, self.retryable = _ERROR_META.get(code, (400, False))
        super().__init__(message)


class WorkspacePush:
    """把某工作区当前分支的提交推送到它绑定的远端仓库。"""

    def __init__(
        self,
        sandboxes: SandboxService,
        git_credentials: GitCredentials,
        workspaces: WorkspaceStore,
    ) -> None:
        self._sandboxes = sandboxes
        self._credentials = git_credentials
        self._workspaces = workspaces

    async def remote_url(self, user_id: str, workspace_id: str) -> str:
        """读取并校验工作区绑定的远端地址（公网 https/ssh，过 SSRF 校验）。"""
        record = await self._workspaces.get(user_id, workspace_id)
        if record is None:
            raise PushRejected("workspace_not_found", "workspace not found")
        remote = str((record.get("metadata") or {}).get(GIT_REMOTE_KEY) or "").strip()
        if not remote:
            raise PushRejected(
                "git_remote_missing",
                "该工作区尚未绑定 Git 仓库地址，请先在「设置 → 工作区」填写仓库 URL",
            )
        try:
            assert_safe_clone_url(remote)
        except CloneUrlRejected as exc:
            raise PushRejected("invalid_remote", str(exc)) from exc
        return remote

    async def push(
        self,
        user_id: str,
        session_id: str,
        workspace_id: str,
        branch: str = "",
        remote_branch: str = "",
    ) -> dict:
        """导出沙箱内 `branch` 的提交，推送到远端 `remote_branch`。

        源/目标分支解耦：`branch` 留空用仓库当前分支，`remote_branch` 留空则与
        `branch` 同名。两者可以不同（本地 master → 远端 main / 远端 docs/readme）。

        前置条件（任一缺失都抛 PushRejected，且标记为不可重试）：
        工作区存在且绑定了远端、用户已连接 GitHub、当前会话有沙箱、仓库有分支。
        """
        remote_url = await self.remote_url(user_id, workspace_id)

        token = await self._credentials.load_token(user_id)
        if not token:
            raise PushRejected(
                "git_credential_missing",
                "尚未连接 GitHub，请先在「设置」中填入 Personal Access Token",
            )

        try:
            sandbox = await self._sandboxes.existing_workspace(user_id, session_id)
        except SandboxNotFound as exc:
            raise PushRejected(
                "sandbox_not_found",
                "当前会话还没有沙箱，请先在对话里让 Agent 运行一次",
            ) from exc

        work_path = WORKSPACE.workspace_path(workspace_id)
        source_branch = branch.strip() or await current_branch(sandbox, work_path)
        if not source_branch:
            raise PushRejected(
                "no_branch",
                "无法确定分支（工作区可能还不是 git 仓库或还没有提交）",
            )
        target_branch = remote_branch.strip() or source_branch

        try:
            bundle = await export_bundle(sandbox, work_path, source_branch)
        except BundleExportError as exc:
            raise PushRejected("bundle_export_failed", str(exc)) from exc

        try:
            result = await push_bundle(
                bundle, remote_url, source_branch, token, target_branch=target_branch
            )
        except PushNonFastForward as exc:
            raise PushRejected("push_non_fast_forward", str(exc)) from exc
        except PushError as exc:
            raise PushRejected("push_failed", str(exc)) from exc

        logger.info(
            "git_push ok user=%s session=%s workspace=%s src=%s dst=%s bytes=%d",
            user_id,
            session_id,
            workspace_id,
            source_branch,
            target_branch,
            len(bundle),
        )
        return {**result, "bundleBytes": len(bundle)}

    async def remote_info(self, user_id: str, workspace_id: str) -> dict:
        """只读：远端有哪些分支、默认分支是哪个（推送前先看清再决定目标）。"""
        remote_url = await self.remote_url(user_id, workspace_id)
        token = await self._credentials.load_token(user_id)
        if not token:
            raise PushRejected(
                "git_credential_missing",
                "尚未连接 GitHub，请先在「设置」中填入 Personal Access Token",
            )
        try:
            return await list_remote_branches(remote_url, token)
        except PushError as exc:
            raise PushRejected("remote_query_failed", str(exc)) from exc


__all__ = ["GIT_REMOTE_KEY", "PushRejected", "WorkspacePush"]
