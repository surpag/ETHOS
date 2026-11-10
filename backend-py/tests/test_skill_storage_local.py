"""本地用户 Skill 后端单测（不依赖真实 E2B）。

钉住的是这次改造引入的几条不变量：
- 索引以目录为准自愈，且文本格式与 `sync_overview.py` 逐字节同构
- 「用户没有 Skill」与「存储不可用」是两个不同状态，不能都压成空字符串
- 派生的 overview.md 不进同步（否则「拉回旧索引→写回→再推」来回抖动）
- 本地删掉的 Skill 会传播到沙箱；但「整体消失」按可疑处理，不传播
- pull 覆盖前若本地被改过，先留存快照（不静默丢内容）
- 用户之间目录隔离
"""

from __future__ import annotations

import asyncio
import shutil

from fake_skill_sandbox import MOUNT, FakeSkillSandbox, skill_md

from edith_backend.internal.skills.storage import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    SkillStorageService,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend

USER = "user_local_abc"
OTHER = "user_local_xyz"


def _make(tmp_path) -> tuple[LocalSkillBackend, SkillStorageService]:
    backend = LocalSkillBackend(tmp_path / "user-skills")
    return backend, SkillStorageService(backend)


def _write_skill(backend: LocalSkillBackend, user: str, name: str, description: str) -> None:
    target = backend.user_dir(user) / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(skill_md(name, description), encoding="utf-8")


# ---- 索引 ----

def test_read_index_heals_and_matches_script_format(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "demo", "演示用能力")
        # 一份过期索引：必须被目录扫描结果纠正
        stale = backend.user_dir(USER) / "overview.md"
        stale.write_text("- `ghost`：已经不存在的技能\n", encoding="utf-8")

        index = await service.read_index(USER)

        assert index.status == STATUS_OK
        assert index.healed is True
        assert [s.name for s in index.summaries] == ["demo"]
        assert "- `demo`：演示用能力" in index.text
        assert "路径：`skills/custom/demo/SKILL.md`" in index.text
        # 与 sync_overview.py 的头部格式一致（同一份文本喂给模型）
        assert index.text.startswith("# 用户 Skills 总览")
        assert "ghost" not in stale.read_text(encoding="utf-8")

    asyncio.run(go())


def test_empty_index_is_not_reported_as_failure(tmp_path) -> None:
    async def go() -> None:
        _, service = _make(tmp_path)
        index = await service.read_index(USER)
        assert index.status == STATUS_EMPTY
        assert index.reason == ""
        assert index.summaries == []

    asyncio.run(go())


def test_unwritable_root_is_reported_as_unavailable(tmp_path) -> None:
    async def go() -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        service = SkillStorageService(LocalSkillBackend(blocked))
        index = await service.read_index(USER)
        # 关键：不是空字符串静默通过，而是明确 unavailable + 原因
        assert index.status == STATUS_UNAVAILABLE
        assert index.reason

    asyncio.run(go())


def test_invalid_skill_dir_is_skipped_but_reported(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "good", "可用")
        broken = backend.user_dir(USER) / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")

        index = await service.read_index(USER)

        assert [s.name for s in index.summaries] == ["good"]
        assert "broken" in index.reason

    asyncio.run(go())


# ---- 同步 ----

def test_push_materializes_skill_in_new_sandbox(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "demo", "演示用能力")
        sandbox = FakeSkillSandbox("sbx-a")

        result = await service.push(USER, sandbox)

        assert result.status == STATUS_OK
        assert sandbox.has("demo/SKILL.md")
        # 派生索引与忽略文件不进沙箱：沙箱里不应该有第二个「权威索引」
        assert not sandbox.has("overview.md")
        assert not sandbox.has(".edithignore")

    asyncio.run(go())


def test_pull_imports_writes_that_bypass_write_file(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        # 模拟 run_command / 脚本直接落盘（sandbox_write_file 之外的通路）
        sandbox.put("news/SKILL.md", skill_md("news", "抓新闻"))

        result = await service.pull(USER, sandbox)

        assert result.pulled == 1
        assert (backend.user_dir(USER) / "news" / "SKILL.md").is_file()

    asyncio.run(go())


def test_pull_does_not_delete_what_the_sandbox_never_saw(tmp_path) -> None:
    """过期沙箱不许当「删除」的证人。

    真实事故：宿主有 14 个 Skill，某沙箱从没见过其中最新那个（它的上次同步早于
    那次创建），一轮回拉把宿主裁到 13。回拉只允许「吸收新增/修改」，删除必须
    先证明这份候选见过当前权威内容。
    """

    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "old", "沙箱见过的")
        _write_skill(backend, USER, "brand-new", "沙箱从没见过")
        sandbox = FakeSkillSandbox("sbx-a")
        # 沙箱里只有 old —— 等价于它的内容停在创建 brand-new 之前
        sandbox.put("old/SKILL.md", skill_md("old", "沙箱见过的"))

        result = await service.pull(USER, sandbox)

        assert (backend.user_dir(USER) / "brand-new" / "SKILL.md").is_file()
        assert result.deleted == 0
        assert "suppressed" in result.reason

        # 同一份过期候选再来一次也不能删：被压制的回拉不算同步成功，
        # 基线不能推进，否则第二轮就会拿「基线 == 本地」放行删除。
        again = await service.pull(USER, sandbox)
        assert again.deleted == 0
        assert (backend.user_dir(USER) / "brand-new" / "SKILL.md").is_file()

    asyncio.run(go())


def test_pull_deletes_when_sandbox_saw_the_current_copy(tmp_path) -> None:
    """删除语义不能因此失效：push 过、本地没再变过 → 沙箱里的删除照常传播。"""

    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "keep", "保留")
        _write_skill(backend, USER, "drop", "用户在沙箱里删掉")
        sandbox = FakeSkillSandbox("sbx-a")
        await service.push(USER, sandbox)

        # 模拟会话里用 run_command / 文件面板删掉整个目录
        await sandbox.files.remove(f"{MOUNT}/drop/SKILL.md")

        result = await service.pull(USER, sandbox)

        assert not (backend.user_dir(USER) / "drop").exists()
        assert result.deleted == 1

    asyncio.run(go())


def test_cancelled_pull_leaves_no_staging(tmp_path) -> None:
    """取消（用户中止 / 任务被 cancel）也要清掉暂存目录。

    `CancelledError` 不是 `Exception`，原来的错误分支抓不到它，实测会在权威目录
    里留下一个 `.staging-<ts>`（里面是半棵树）。
    """

    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "demo", "演示用能力")
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.list_error = asyncio.CancelledError()

        try:
            await service.pull(USER, sandbox)
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - 取消必须向上传播，不能被吞成普通结果
            raise AssertionError("cancellation must propagate")

        assert list(backend.user_dir(USER).glob(".staging-*")) == []

    asyncio.run(go())


def test_local_delete_propagates_to_sandbox(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "keep", "保留")
        _write_skill(backend, USER, "drop", "待删除")
        sandbox = FakeSkillSandbox("sbx-a")
        await service.push(USER, sandbox)
        assert sandbox.has("drop/SKILL.md")

        shutil.rmtree(backend.user_dir(USER) / "drop")
        result = await service.push(USER, sandbox)

        assert result.deleted >= 1
        assert not sandbox.has("drop/SKILL.md")
        assert sandbox.has("keep/SKILL.md")

    asyncio.run(go())


def test_full_wipe_is_not_propagated_to_sandbox(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "only", "唯一技能")
        sandbox = FakeSkillSandbox("sbx-a")
        await service.push(USER, sandbox)

        # 本地目录被整体清空（盘未挂载 / 误操作）——不应把沙箱也删空
        shutil.rmtree(backend.user_dir(USER) / "only")
        result = await service.push(USER, sandbox)

        assert result.deleted == 0
        assert sandbox.has("only/SKILL.md")

    asyncio.run(go())


def test_remote_list_failure_is_unavailable_not_silent(tmp_path) -> None:
    async def go() -> None:
        _, service = _make(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.list_error = RuntimeError("sandbox unavailable")

        result = await service.pull(USER, sandbox)

        assert result.status == STATUS_UNAVAILABLE
        assert "sandbox unavailable" in result.reason

    asyncio.run(go())


def test_push_without_remote_view_is_partial(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "demo", "演示用能力")
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.list_error = RuntimeError("list timeout")

        result = await service.push(USER, sandbox)

        # 推送仍可能成功，但不能报「一切正常」——降级必须可见
        assert result.status == STATUS_PARTIAL
        assert sandbox.has("demo/SKILL.md")

    asyncio.run(go())


def test_pull_stashes_local_copy_before_overwriting_diverged_version(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "demo", "v1")
        sandbox_a = FakeSkillSandbox("sbx-a")
        sandbox_b = FakeSkillSandbox("sbx-b")
        await service.push(USER, sandbox_a)
        await service.push(USER, sandbox_b)

        # 会话 A 与 B 各自改了同一个 Skill
        sandbox_a.put("demo/SKILL.md", skill_md("demo", "v2-from-a"))
        sandbox_b.put("demo/SKILL.md", skill_md("demo", "v3-from-b"))

        await service.pull(USER, sandbox_a)
        result = await service.pull(USER, sandbox_b)

        # 最终本地是 B 的版本（最后写入者胜出），但 A 的版本必须能找回来
        local = (backend.user_dir(USER) / "demo" / "SKILL.md").read_text(encoding="utf-8")
        assert "v3-from-b" in local
        assert result.conflicts, "覆盖前应留存快照并上报冲突"
        stashed = backend.user_dir(USER) / result.conflicts[0] / "demo" / "SKILL.md"
        assert "v2-from-a" in stashed.read_text(encoding="utf-8")

    asyncio.run(go())


def test_users_are_isolated(tmp_path) -> None:
    async def go() -> None:
        backend, service = _make(tmp_path)
        _write_skill(backend, USER, "private", "只属于 A")

        assert backend.user_dir(USER) != backend.user_dir(OTHER)
        index = await service.read_index(OTHER)
        assert index.summaries == []
        assert not (backend.user_dir(OTHER) / "private").exists()

    asyncio.run(go())


def test_unsafe_user_id_cannot_escape_root(tmp_path) -> None:
    async def go() -> None:
        backend, _ = _make(tmp_path)
        # 越目录写入的经典形态：路径分隔符 / 上跳
        for raw in ("../evil", "a/b", "..", ""):
            try:
                directory = backend.user_dir(raw)
            except ValueError:
                continue
            assert tmp_path in directory.parents or directory.parent == tmp_path / "user-skills"

    asyncio.run(go())
