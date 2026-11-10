"""B2-1 接线：Skill 同步按作用域分发（用户级 / 项目级）。

本阶段只做**接线**，不改解析与注入：项目级 Skill 会被推进沙箱、也会被回拉，
但注入文本仍是 B0 的两段格式（`resolver` 尚未读项目层，那是 B2-2）。

验收点（每条都对应一个「不接就会静默错」的失效方式）：

1. 有工作区上下文时，项目级与用户级**各自独立**推送/回拉，互不覆盖
   （分层目录 + 独立根 + 独立共识集合，D4/D7）；
2. 没有工作区上下文时项目级不参与（纯聊天会话只有用户级 Skill）；
3. 项目级后端结构性不可用（`e2b`）时**不尝试**——
   它每次调用都返回 unavailable 而不抛错，接在这里等于每次工具调用都空跑一次；
4. 归属校验在接线层仍然 fail-closed：无权时**一个目录都不建**；
5. 待回拉的键里带 `workspace_id`：会话换绑工作区后，兑现时刻仍能拉对项目；
6. 工具层判定命中哪一层，就只拉那一层（多拉一层 = 每次写用户级 Skill 都多一次远端比对）。
"""

from __future__ import annotations

import asyncio

import pytest
from fake_skill_sandbox import FakeSkillSandbox, skill_md

from edith_backend.internal.sandbox.service import Service as SandboxService
from edith_backend.internal.skills.storage import (
    SkillScopeStores,
    SkillStorageService,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend
from edith_backend.internal.skills.types import SCOPE_PROJECT, SCOPE_USER

USER = "user_scope_alice"
WS_A = "ws-alpha"
WS_B = "ws-beta"

CUSTOM_MOUNT = "/home/user/skills/custom"


def _scope_files(sandbox: FakeSkillSandbox, workspace_id: str) -> list[str]:
    """沙箱里项目级 Skill 的文件（相对 `skills/project/{ws}`）。"""
    prefix = f"/home/user/skills/project/{workspace_id}/"
    return sorted(
        key[len(prefix) :] for key in sandbox.files.blobs if key.startswith(prefix)
    )


def _custom_files(sandbox: FakeSkillSandbox) -> list[str]:
    prefix = CUSTOM_MOUNT + "/"
    return sorted(
        key[len(prefix) :] for key in sandbox.files.blobs if key.startswith(prefix)
    )


async def _allow(_user_id: str, _workspace_id: str) -> bool:
    return True


async def _deny(_user_id: str, _workspace_id: str) -> bool:
    return False


def _stores(tmp_path, checker=_allow) -> SkillScopeStores:
    user_service = SkillStorageService(
        LocalSkillBackend(tmp_path / "user-skills")
    )
    return SkillScopeStores(
        "local", user_service=user_service, access_checker=checker
    )


def _sandboxes(stores: SkillScopeStores) -> SandboxService:
    """只需 Skill 钩子的 Service 实例（db/volumes 不参与这些路径）。"""
    return SandboxService(
        None,  # type: ignore[arg-type]
        "template",
        None,  # type: ignore[arg-type]
        skill_scopes=stores,
    )


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    """项目级权威根默认是 cwd 下的 `project-skills/`，测试必须改道到 tmp。"""
    monkeypatch.setenv("EDITH_PROJECT_SKILLS_DIR", str(tmp_path / "project-skills"))
    return tmp_path / "project-skills"


# ---- 双作用域推送 ---------------------------------------------------------------


def test_push_covers_both_scopes_in_separate_dirs(tmp_path) -> None:
    async def go() -> None:
        stores = _stores(tmp_path)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")

        # 两条来源：沙箱里已存在用户级与项目级的写入 → 分别回拉到各自的权威副本
        sandbox.put("user-skill/SKILL.md", skill_md("user-skill", "用户级"))
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS_A}/proj-skill/SKILL.md",
            skill_md("proj-skill", "项目级").encode(),
        )
        await sandboxes.pull_skills(USER, WS_A, sandbox)

        project_root = tmp_path / "project-skills" / USER / WS_A
        assert (project_root / "proj-skill" / "SKILL.md").is_file()
        assert (
            tmp_path / "user-skills" / USER / "user-skill" / "SKILL.md"
        ).is_file()
        # 独立根：项目级不会跑到用户级目录下，反之亦然
        assert not (tmp_path / "user-skills" / USER / "proj-skill").exists()
        assert not (project_root / "user-skill").exists()

        # 换个干净沙箱做推送：两层分别落到各自的沙箱目录
        fresh = FakeSkillSandbox("sbx-b")
        await sandboxes._push_skills(USER, WS_A, fresh)
        assert _custom_files(fresh) == ["user-skill/SKILL.md"]
        assert _scope_files(fresh, WS_A) == ["proj-skill/SKILL.md"]

    asyncio.run(go())


def test_push_skips_project_scope_without_workspace(tmp_path) -> None:
    """纯聊天会话（无工作区）只推用户级；项目级不参与。"""

    async def go() -> None:
        stores = _stores(tmp_path)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.put("only-user/SKILL.md", skill_md("only-user", "用户级"))
        await sandboxes.pull_skills(USER, "", sandbox)

        fresh = FakeSkillSandbox("sbx-b")
        await sandboxes._push_skills(USER, "", fresh)
        assert _custom_files(fresh) == ["only-user/SKILL.md"]
        assert not any(
            key.startswith("/home/user/skills/project") for key in fresh.files.blobs
        )

    asyncio.run(go())


def test_two_workspaces_do_not_share_project_storage(tmp_path) -> None:
    async def go() -> None:
        stores = _stores(tmp_path)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS_A}/a-only/SKILL.md",
            skill_md("a-only", "A 项目").encode(),
        )
        await sandboxes.pull_skills(USER, WS_A, sandbox)

        other = FakeSkillSandbox("sbx-b")
        await sandboxes._push_skills(USER, WS_B, other)

        # B 项目不该看到 A 项目的 Skill（独立根 + 独立沙箱目录）
        assert _scope_files(other, WS_B) == []
        assert _scope_files(other, WS_A) == []

    asyncio.run(go())


# ---- 结构性不可用 / 归属校验 ------------------------------------------------------


def test_unavailable_project_backend_is_not_attempted(tmp_path) -> None:
    """`e2b` 下项目级不可用：同步钩子直接跳过，不做「空跑一次再记 unavailable」。"""

    async def go() -> None:
        stores = _stores(tmp_path)
        e2b = SkillScopeStores("e2b", user_service=stores.user())
        assert e2b.project_supported() is False
        assert e2b.project(WS_A) is not None  # 门面仍在：不可用是后端的事，不是缓存的

        sandboxes = _sandboxes(e2b)
        assert sandboxes._scope_service(SCOPE_PROJECT, WS_A) is None
        assert sandboxes._scope_service(SCOPE_USER, WS_A) is not None

        sandbox = FakeSkillSandbox("sbx-a")
        await sandboxes._push_skills(USER, WS_A, sandbox)  # 不抛、不写项目目录
        assert not any(
            key.startswith("/home/user/skills/project") for key in sandbox.files.blobs
        )

    asyncio.run(go())


def test_access_denied_creates_no_project_dirs(tmp_path) -> None:
    """fail-closed 在接线层仍然成立：无权 = 一个目录都不建。"""

    async def go() -> None:
        stores = _stores(tmp_path, checker=_deny)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")

        await sandboxes._push_skills(USER, WS_A, sandbox)
        await sandboxes.pull_skills(USER, WS_A, sandbox)

        project_root = tmp_path / "project-skills"
        assert not project_root.exists() or not any(project_root.iterdir())

    asyncio.run(go())


# ---- 待回拉标记：键里必须带 workspace -------------------------------------------


def test_pending_key_carries_workspace_id(tmp_path) -> None:
    stores = _stores(tmp_path)
    sandboxes = _sandboxes(stores)

    sandboxes.mark_skill_pull_pending(USER, "sess-1", WS_A)
    sandboxes.mark_skill_pull_pending(USER, "sess-1", "")
    sandboxes.mark_skill_pull_pending(USER, "sess-2", WS_B)

    assert sandboxes._pending_workspaces(USER, "sess-1") == ["", WS_A]
    assert sandboxes._pending_workspaces(USER, "sess-2") == [WS_B]
    # 摘掉后不再重复兑现（否则下一轮会重放一次早已不存在的写入）
    assert sandboxes._take_pending(USER, "sess-1") == ["", WS_A]
    assert sandboxes._pending_workspaces(USER, "sess-1") == []


def test_flush_uses_workspace_recorded_at_mark_time(tmp_path) -> None:
    """会话换绑工作区后，收尾仍按**挂起时**的工作区回拉，而不是猜当前的。"""

    async def go() -> None:
        stores = _stores(tmp_path)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS_A}/late/SKILL.md",
            skill_md("late", "命令写的").encode(),
        )

        async def fake_existing(_user_id: str, _session_id: str):
            return sandbox

        sandboxes.existing_workspace = fake_existing  # type: ignore[method-assign]

        sandboxes.mark_skill_pull_pending(USER, "sess-1", WS_A)
        await sandboxes.flush_skill_sync(USER, "sess-1")

        assert (
            tmp_path / "project-skills" / USER / WS_A / "late" / "SKILL.md"
        ).is_file()

    asyncio.run(go())


# ---- 只拉命中的那一层 -----------------------------------------------------------


def test_pull_only_touches_requested_scope(tmp_path) -> None:
    """写用户级 Skill 时不该顺带比对项目级的整棵树。"""

    async def go() -> None:
        stores = _stores(tmp_path)
        sandboxes = _sandboxes(stores)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.put("u/SKILL.md", skill_md("u", "用户级"))
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS_A}/p/SKILL.md",
            skill_md("p", "项目级").encode(),
        )

        await sandboxes.pull_skills(USER, WS_A, sandbox, scopes={SCOPE_USER})

        assert (tmp_path / "user-skills" / USER / "u" / "SKILL.md").is_file()
        assert not (tmp_path / "project-skills" / USER / WS_A / "p").exists()

    asyncio.run(go())


# ---- 沙箱目录：不押远端递归建父目录 ---------------------------------------------


def test_push_builds_project_parent_dirs(tmp_path) -> None:
    """项目级 push 必须自己逐级建目录，不能指望 `make_dir` 递归补父目录。

    沙箱模板只 `make_dir` 了 `skills/{system,custom}`，`skills/project` 与
    `skills/project/{ws}` 两级都不存在。假沙箱默认会补齐所有祖先，恰好把
    「押远端递归」这类 bug 掩盖掉，所以这里开严格模式（父目录缺失即报错）。

    失败形态值得为它单独写一条断言：`_ensure_sandbox_dir` 吞掉异常，push 随后
    返回 unavailable 只留一行 warning，模型看到的是「这个项目没有 Skill」。
    """

    async def go() -> FakeSkillSandbox:
        stores = _stores(tmp_path)
        # 权威副本先有内容，push 才会真正走文件写入
        skill_dir = tmp_path / "project-skills" / USER / WS_A / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(skill_md("demo", "项目级"), encoding="utf-8")

        sandbox = FakeSkillSandbox("sbx-p")
        sandbox.files.strict_mkdir = True
        await stores.project(WS_A).push(USER, sandbox)
        return sandbox

    sandbox = asyncio.run(go())
    project_root = f"/home/user/skills/project/{WS_A}"
    assert "/home/user/skills/project" in sandbox.files.dirs
    assert project_root in sandbox.files.dirs
    assert f"{project_root}/demo/SKILL.md" in sandbox.files.blobs
