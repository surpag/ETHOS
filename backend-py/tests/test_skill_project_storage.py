"""B1：项目级 Skill 存储（独立根、状态键隔离、沙箱路径带 workspace_id）与路径守卫。

这一层的验收不看「能不能读到一个 Skill」，而看**隔离**是否成立：

- 同一用户的 A 项目与 B 项目互不可见；
- 同一项目的两个用户互不可见；
- 用户级与项目级的状态键不同名（否则删除传播会越界，D3 的恢复就没有源）；
- 沙箱路径里带 workspace_id（否则同一沙箱服务两个项目时会串味）。

不依赖真实 E2B：`LocalSkillBackend` 与 `ProjectSkillBackend` 都是宿主目录实现，
沙箱侧只在 push/pull 时用到，本文件不覆盖那部分（见 `test_skill_cross_session.py`）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fake_skill_sandbox import skill_md

from edith_backend.internal.sandbox.workspace import workspace_path
from edith_backend.internal.skills.storage import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    SkillScopeStores,
    new_scope_backend,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend
from edith_backend.internal.skills.storage.project import ProjectSkillBackend
from edith_backend.internal.skills.storage.scopes import _UnavailableBackend

ALICE = "user_alice"
BOB = "user_bob"
WS_A = "ws-alpha"
WS_B = "ws-beta"


def _write_skill(root, dir_name: str, name: str, description: str) -> None:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md(name, description), encoding="utf-8")


async def _allow_all(user_id: str, workspace_id: str) -> bool:
    """本文件只验隔离，不验归属；归属判定在 `test_skill_delivery_transaction.py`。"""
    return True


def _backend(
    tmp_path, workspace_id: str = WS_A, *, access_checker=_allow_all
) -> ProjectSkillBackend:
    return ProjectSkillBackend(
        workspace_id,
        root=str(tmp_path / "project-skills"),
        access_checker=access_checker,
    )


# --------------------------------------------------------------------------
# 一、布局与键：两处隔离都是「结构性质」，不是靠调用方自觉
# --------------------------------------------------------------------------
def test_layout_paths_carry_workspace_id(tmp_path) -> None:
    backend = _backend(tmp_path)
    assert backend.user_dir(ALICE) == tmp_path / "project-skills" / ALICE / WS_A
    assert backend.sandbox_root == f"/home/user/skills/project/{WS_A}"
    # 索引里的路径行必须与沙箱目录同源，否则模型按索引读文件会落空
    assert backend.index_base_path == f"skills/project/{WS_A}"


def test_state_key_isolated_from_user_scope_and_other_workspace(tmp_path) -> None:
    user_backend = new_scope_backend("user", "local")
    a = _backend(tmp_path, WS_A)
    b = _backend(tmp_path, WS_B)

    keys = {
        user_backend.state_key(ALICE),
        a.state_key(ALICE),
        b.state_key(ALICE),
    }
    # 三套共识集合必须互不同名：共用会让「删项目级」被推演成「用户级也删了」，
    # 而 D3 的「项目级删除后恢复用户级」正是靠两份集合互不干扰。
    assert len(keys) == 3
    assert a.state_key(ALICE) == f"project-skills-{WS_A}-{ALICE}"


def test_empty_workspace_id_is_rejected_not_silently_empty(tmp_path) -> None:
    with pytest.raises(ValueError):
        ProjectSkillBackend("", root=str(tmp_path))
    with pytest.raises(ValueError):
        ProjectSkillBackend("   ", root=str(tmp_path))


# --------------------------------------------------------------------------
# 二、索引：路径行用真实目录名，且 base_path 随作用域变化
# --------------------------------------------------------------------------
def test_index_renders_project_base_path(tmp_path) -> None:
    backend = _backend(tmp_path)
    _write_skill(backend.user_dir(ALICE), "demo", "demo", "演示用")

    index = asyncio.run(backend.read_index(ALICE))
    assert index.status == STATUS_OK
    assert "- `demo`：演示用" in index.text
    assert f"路径：`skills/project/{WS_A}/demo/SKILL.md`" in index.text
    assert [s.name for s in index.summaries] == ["demo"]


def test_index_path_uses_directory_name_not_frontmatter_name(tmp_path) -> None:
    """目录名与 frontmatter name 不一致时，路径必须是**目录名**。

    这是 B1 修掉的真实缺陷：旧实现两处都用 frontmatter 的 `name` 拼路径，
    于是索引会写出一个不存在的目录，模型读它得到的是「空的 Skill」而不是报错。
    """
    backend = _backend(tmp_path)
    _write_skill(backend.user_dir(ALICE), "on-disk-dir", "display-name", "说明")

    index = asyncio.run(backend.read_index(ALICE))
    # 显示名取 frontmatter
    assert "- `display-name`：说明" in index.text
    # 路径取真实目录
    assert f"路径：`skills/project/{WS_A}/on-disk-dir/SKILL.md`" in index.text
    assert "display-name/SKILL.md" not in index.text


def test_user_scope_index_unchanged_when_dir_equals_name(tmp_path) -> None:
    """常见情况（目录名 == name）必须与改造前逐字节一致，否则等于全量缓存失效。"""
    backend = LocalSkillBackend(tmp_path / "user-skills")
    _write_skill(backend.user_dir(ALICE), "demo", "demo", "演示用")

    index = asyncio.run(backend.read_index(ALICE))
    assert index.text == (
        "# 用户 Skills 总览\n"
        "\n"
        "以下内容由脚本生成，请勿手动修改。\n"
        "\n"
        "- `demo`：演示用\n"
        "  - 路径：`skills/custom/demo/SKILL.md`"
    )


# --------------------------------------------------------------------------
# 三、隔离本身
# --------------------------------------------------------------------------
def test_two_workspaces_of_same_user_are_isolated(tmp_path) -> None:
    a = _backend(tmp_path, WS_A)
    b = _backend(tmp_path, WS_B)
    _write_skill(a.user_dir(ALICE), "only-a", "only-a", "A 项目专用")

    assert [s.name for s in asyncio.run(a.read_index(ALICE)).summaries] == ["only-a"]
    assert asyncio.run(b.read_index(ALICE)).status == STATUS_EMPTY
    assert asyncio.run(b.read_index(ALICE)).summaries == []


def test_two_users_of_same_workspace_are_isolated(tmp_path) -> None:
    backend = _backend(tmp_path)
    _write_skill(backend.user_dir(ALICE), "alice-only", "alice-only", "Alice 的")

    assert [s.name for s in asyncio.run(backend.read_index(ALICE)).summaries] == [
        "alice-only"
    ]
    assert asyncio.run(backend.read_index(BOB)).summaries == []


def test_project_and_user_roots_do_not_overlap(tmp_path) -> None:
    """项目级不能落在 user-skills/ 之下：用户级 push 会把它一起推走。"""
    project = _backend(tmp_path)
    user = LocalSkillBackend(tmp_path / "user-skills")

    project_root = project.user_dir(ALICE)
    user_root = user.user_dir(ALICE)
    assert not str(project_root).startswith(str(user_root))
    assert not str(user_root).startswith(str(project_root))


# --------------------------------------------------------------------------
# 四、作用域分发：不可用要说清原因，拼错作用域不许静默降级
# --------------------------------------------------------------------------
def test_scope_factory_rejects_unknown_scope() -> None:
    backend = new_scope_backend("session", "local")
    assert isinstance(backend, _UnavailableBackend)
    index = asyncio.run(backend.read_index(ALICE))
    assert index.status == STATUS_UNAVAILABLE
    assert "unknown skill scope" in index.reason


def test_project_scope_unavailable_on_e2b_backend() -> None:
    backend = new_scope_backend("project", "e2b", WS_A)
    index = asyncio.run(backend.read_index(ALICE))
    assert index.status == STATUS_UNAVAILABLE
    assert "require local backend" in index.reason
    # 空列表 + unavailable 是两件事：不能假装「这个项目没有 Skill」
    assert index.summaries == []


def test_scope_stores_cache_per_workspace(tmp_path) -> None:
    stores = SkillScopeStores("local", volumes=None)
    first = stores.project(WS_A)
    assert stores.project(WS_A) is first
    assert stores.project(WS_B) is not first
    with pytest.raises(ValueError):
        stores.project("")


def test_scope_stores_reuse_injected_user_service() -> None:
    from edith_backend.internal.skills.storage import SkillStorageService

    service = SkillStorageService(new_scope_backend("user", "local"))
    stores = SkillScopeStores("local", user_service=service)
    assert stores.user() is service


# --------------------------------------------------------------------------
# 五、路径守卫：项目级与 workspace/ 同构
# --------------------------------------------------------------------------
def test_guard_allows_own_project_path() -> None:
    path = workspace_path(f"skills/project/{WS_A}/demo/SKILL.md", False, WS_A)
    assert path.endswith(f"skills/project/{WS_A}/demo/SKILL.md")


def test_guard_denies_other_workspace_project_path() -> None:
    with pytest.raises(PermissionError):
        workspace_path(f"skills/project/{WS_B}/demo/SKILL.md", False, WS_A)


def test_guard_denies_project_path_without_workspace_context() -> None:
    """纯聊天会话没有 workspace 上下文：项目级 Skill 本来就不会被注入，放行等于留后门。"""
    with pytest.raises(PermissionError):
        workspace_path(f"skills/project/{WS_A}/demo/SKILL.md", False, "")


def test_guard_expands_bare_namespace_to_current_workspace() -> None:
    """`skills/project` 与 `workspace` 都只有一个合法取值：当前会话的工作区。

    这两条路径是文件面板里的可展开节点，模型也会先 ls 再进。要求它们自带 ID 段
    等于给调用方出一条无法满足的指令——补上 ID 后又因为没有上下文被拒，
    两条错误互相矛盾，任何路径都走不通（线上真实故障）。
    """
    assert workspace_path("skills/project", True, WS_A) == (
        f"/home/user/skills/project/{WS_A}"
    )
    assert workspace_path("skills/project/", True, WS_A) == (
        f"/home/user/skills/project/{WS_A}"
    )
    assert workspace_path("workspace", True, WS_A) == f"/home/user/workspace/{WS_A}"


def test_guard_bare_namespace_denied_without_workspace_context() -> None:
    """没有工作区上下文时，两个命名空间指向哪里都无从判断 → 同一句话拒绝。"""
    for path in ("skills/project", "workspace"):
        with pytest.raises(PermissionError) as exc:
            workspace_path(path, True, "")
        assert "without a workspace context" in str(exc.value), path


def test_file_tools_receive_workspace_context() -> None:
    """文件类工具必须带着会话的工作区进入执行层。

    曾经的形态：`execute()` 把 workspace_id 交给授权校验（`_scope_violation`），
    却没交给 `_op_*`，于是 `_op_*` 里 `workspace_path()` 的 current_workspace_id
    恒为空 → `skills/project/{ws}/…` 一律被判「没有 workspace 上下文」，
    项目级 Skill 在对话里永远写不进去。假沙箱不校验路径，只有这条链路能暴露它。
    """

    class _Files:
        def __init__(self) -> None:
            self.listed: list[str] = []

        async def list(self, directory: str, depth: int = 1):
            self.listed.append(directory)
            return []

    class _Service:
        def __init__(self, sandbox) -> None:
            self._sandbox = sandbox

        async def workspace(self, user_id, session_id, workspace_id=""):
            return self._sandbox

    async def go() -> None:
        from edith_backend.internal.sandbox.tools import SandboxToolSet

        files = _Files()
        tools = SandboxToolSet(_Service(SimpleNamespace(files=files)))

        raw = await tools.execute(
            "sandbox_list_files",
            {"path": f"skills/project/{WS_A}"},
            "user_alice",
            "session-1",
            WS_A,
        )
        assert "error" not in json.loads(raw), raw
        assert files.listed == [f"/home/user/skills/project/{WS_A}"]

        # 纯聊天会话（无工作区）：同一条路径必须报「没有上下文」，而不是落进沙箱
        raw = await tools.execute(
            "sandbox_list_files", {"path": "skills/project"}, "user_alice", "session-2", ""
        )
        assert "workspace context" in json.loads(raw)["error"], raw

    asyncio.run(go())


def test_guard_leaves_user_and_system_skill_paths_alone() -> None:
    assert workspace_path("skills/custom/demo/SKILL.md", False, "").endswith(
        "skills/custom/demo/SKILL.md"
    )
    assert workspace_path("skills/system/pdf/SKILL.md", False, "").endswith(
        "skills/system/pdf/SKILL.md"
    )
