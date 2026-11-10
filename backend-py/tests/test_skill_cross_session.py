"""用户级 Skill 跨会话端到端测试（不依赖真实 E2B）。

对应验收清单：
1. 会话 A 创建 Skill → 会话 B 能发现（索引里有）**并且**能读到文件
2. 会话 A 修改 Skill（等长改写）→ 会话 B 读到最新版本
3. 沙箱销毁重建后 Skill 仍存在（权威副本在宿主，不在沙箱临时盘）
4. 不同用户之间互相不可见
5. 同步失败时有明确状态与错误原因（不是空字符串静默）
6. `run_command` 之类绕过 write_file 的写入，靠 pull 兜底也能落地
7. 两个会话并发改同一个 Skill：不丢内容（旧版本留存快照）
8. 沙箱内容整体消失（被 rm -rf）不反向删光本地

「会话」用「不同 sandbox_id 的同一个用户」表达：这正是现实里两个会话各自
拥有一个沙箱、共享同一份用户级 Skill 的形态。
"""

from __future__ import annotations

import asyncio

from fake_skill_sandbox import FakeSkillSandbox, skill_md

from edith_backend.internal.sandbox.service import Service as SandboxService
from edith_backend.internal.sandbox.tools import _touched_skill_scopes
from edith_backend.internal.skills.catalog import Catalog
from edith_backend.internal.skills.storage import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    SkillScopeStores,
    SkillStorageService,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend
from edith_backend.internal.skills.types import SCOPE_PROJECT, SCOPE_USER

USER = "user_e2e_alice"
OTHER = "user_e2e_bob"


def _setup(tmp_path):
    backend = LocalSkillBackend(tmp_path / "user-skills")
    service = SkillStorageService(backend)
    return backend, service, Catalog(service)


def test_session_a_creates_skill_and_session_b_finds_it(tmp_path) -> None:
    async def go() -> None:
        backend, service, catalog = _setup(tmp_path)
        session_a = FakeSkillSandbox("sbx-a")
        session_b = FakeSkillSandbox("sbx-b")

        # 会话 A：Agent 用 sandbox_write_file 落盘，工具钩子随后 pull 落地
        session_a.put("reporter/SKILL.md", skill_md("reporter", "写周报"))
        await service.pull(USER, session_a)

        # 会话 B：开始前 push（把权威副本铺进自己的沙箱），再读索引
        await service.push(USER, session_b)
        index = await catalog.read_index(USER)

        names = [s.name for s in index.summaries]
        assert index.status == STATUS_OK
        assert names == ["reporter"]
        # 关键：不只知道名字，**文件也真的在 B 的沙箱里**
        assert session_b.has("reporter/SKILL.md")
        assert "写周报" in session_b.get("reporter/SKILL.md")
        assert (backend.user_dir(USER) / "reporter" / "SKILL.md").is_file()

    asyncio.run(go())


def test_session_b_sees_latest_version_after_equal_length_edit(tmp_path) -> None:
    async def go() -> None:
        _, service, catalog = _setup(tmp_path)
        session_a = FakeSkillSandbox("sbx-a")
        session_b = FakeSkillSandbox("sbx-b")

        session_a.put("reporter/SKILL.md", skill_md("reporter", "能力甲"))
        await service.pull(USER, session_a)
        await service.push(USER, session_b)

        # 等长改写：字节数完全一样，靠大小比对会漏，必须按内容判等
        session_a.put("reporter/SKILL.md", skill_md("reporter", "能力乙"))
        pull_result = await service.pull(USER, session_a)
        assert pull_result.pulled == 1

        # B 下一次拿沙箱时 push，应该把新版本送进去（同样要按内容判等）
        push_result = await service.push(USER, session_b)
        assert push_result.pushed == 1
        assert "能力乙" in session_b.get("reporter/SKILL.md")
        index = await catalog.read_index(USER)
        assert "能力乙" in index.text

    asyncio.run(go())


def test_skill_survives_sandbox_recreation(tmp_path) -> None:
    async def go() -> None:
        _, service, catalog = _setup(tmp_path)
        first = FakeSkillSandbox("sbx-first")
        first.put("persist/SKILL.md", skill_md("persist", "要活过重建"))
        await service.pull(USER, first)

        # 沙箱被销毁：新沙箱是空盘，只有权威副本能把它带回来
        recreated = FakeSkillSandbox("sbx-recreated")
        await service.push(USER, recreated)

        assert recreated.has("persist/SKILL.md")
        index = await catalog.read_index(USER)
        assert [s.name for s in index.summaries] == ["persist"]

    asyncio.run(go())


def test_users_do_not_see_each_other(tmp_path) -> None:
    async def go() -> None:
        backend, service, catalog = _setup(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.put("secret/SKILL.md", skill_md("secret", "只属于 alice"))
        await service.pull(USER, sandbox)

        bob_box = FakeSkillSandbox("sbx-bob")
        await service.push(OTHER, bob_box)
        bob_index = await catalog.read_index(OTHER)

        assert bob_index.status == STATUS_EMPTY
        assert bob_index.summaries == []
        assert not bob_box.has("secret/SKILL.md")
        assert backend.user_dir(USER) != backend.user_dir(OTHER)

    asyncio.run(go())


def test_sync_failure_reports_status_and_reason(tmp_path) -> None:
    async def go() -> None:
        _, service, catalog = _setup(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.files.list_error = RuntimeError("sandbox unavailable")

        result = await service.pull(USER, sandbox)

        assert result.status == STATUS_UNAVAILABLE
        assert result.reason
        # 索引侧仍然可用（本地权威副本在），但要能看出「这次同步失败了」
        index = await catalog.read_index(USER)
        assert index.status == STATUS_EMPTY

    asyncio.run(go())


def test_write_that_bypasses_write_file_is_recovered_by_pull(tmp_path) -> None:
    async def go() -> None:
        backend, service, _ = _setup(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        # 模拟 `sandbox_run_command` 里 heredoc 落盘：dirty 标记根本不会置位
        sandbox.put("via-shell/SKILL.md", skill_md("via-shell", "命令写出来的"))

        result = await service.pull(USER, sandbox)

        assert result.pulled == 1
        assert (backend.user_dir(USER) / "via-shell" / "SKILL.md").is_file()

    asyncio.run(go())


def test_concurrent_sessions_keep_both_versions_recoverable(tmp_path) -> None:
    async def go() -> None:
        backend, service, _ = _setup(tmp_path)
        session_a = FakeSkillSandbox("sbx-a")
        session_b = FakeSkillSandbox("sbx-b")
        session_a.put("demo/SKILL.md", skill_md("demo", "原始"))
        await service.pull(USER, session_a)
        await service.push(USER, session_b)

        session_a.put("demo/SKILL.md", skill_md("demo", "甲改的"))
        session_b.put("demo/SKILL.md", skill_md("demo", "乙改的"))

        results = await asyncio.gather(
            service.pull(USER, session_a), service.pull(USER, session_b)
        )

        local = (backend.user_dir(USER) / "demo" / "SKILL.md").read_text(encoding="utf-8")
        # 两次 pull 被用户级锁串行化：最终收敛到某一个版本（不会写坏/写半）
        assert "甲改的" in local or "乙改的" in local
        # 被覆盖的那一版必须留了快照 —— 不静默丢内容
        stashes = sorted((backend.user_dir(USER) / ".conflicts").glob("*/*/SKILL.md"))
        conflicts = [r for r in results if r.conflicts]
        assert len(stashes) == 1
        assert len(conflicts) == 1
        stashed = stashes[0].read_text(encoding="utf-8")
        assert stashed != local  # 两版都还在：一份在正文，一份在快照
        assert all(r.status == STATUS_OK for r in results)

    asyncio.run(go())


def test_rm_rf_inside_sandbox_does_not_wipe_authoritative_copy(tmp_path) -> None:
    async def go() -> None:
        backend, service, catalog = _setup(tmp_path)
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.put("keep/SKILL.md", skill_md("keep", "不能被沙箱删掉"))
        await service.pull(USER, sandbox)
        await service.push(USER, sandbox)  # 建立共识集合

        # 沙箱侧被整体清空（run_command rm -rf / 沙箱被重置）
        sandbox.files.blobs.clear()
        result = await service.pull(USER, sandbox)

        assert result.deleted == 0
        assert (backend.user_dir(USER) / "keep" / "SKILL.md").is_file()
        index = await catalog.read_index(USER)
        assert [s.name for s in index.summaries] == ["keep"]

    asyncio.run(go())


def test_sandbox_service_pending_pull_is_drained_on_next_touch(tmp_path) -> None:
    async def go() -> None:
        backend, service, _ = _setup(tmp_path)
        # db/volumes 不参与这条路径（只有拿沙箱时才用），这里只验证挂起→兑现
        sandboxes = SandboxService(
            None,  # type: ignore[arg-type]
            "template",
            None,  # type: ignore[arg-type]
            skill_scopes=SkillScopeStores("local", user_service=service),
        )
        sandbox = FakeSkillSandbox("sbx-a")
        sandbox.put("shell-only/SKILL.md", skill_md("shell-only", "命令写的"))

        sandboxes.mark_skill_pull_pending(USER, "sess-1")
        await sandboxes._drain_skill_pending(USER, "sess-1", sandbox)

        assert (backend.user_dir(USER) / "shell-only" / "SKILL.md").is_file()

    asyncio.run(go())


def test_flush_without_pending_is_a_noop(tmp_path) -> None:
    async def go() -> None:
        _, service, _ = _setup(tmp_path)
        sandboxes = SandboxService(
            None,  # type: ignore[arg-type]
            "template",
            None,  # type: ignore[arg-type]
            skill_scopes=SkillScopeStores("local", user_service=service),
        )
        await sandboxes.flush_skill_sync(USER, "sess-1")

    asyncio.run(go())


def test_http_skills_reports_status_besides_list(client) -> None:
    """`/internal/skills` 必须能区分「没有 Skill」与「存储不可用」。

    旧实现只返回一个空数组：用户看到的是「我没有 Skill」，实际可能是存储挂了。
    """
    resp = client.get("/internal/skills", params={"userId": "user_http_probe"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["custom"] == []
    assert body["customStatus"] == "empty"
    assert body["customReason"] == ""
    assert body["system"], "内置 Skill 应照常返回"


# ---- 工具层：判定「这次调用可能改了 Skill」 --------------------------------------


def test_touched_skill_scopes_detects_paths_and_commands() -> None:
    """判定必须回答「改了哪一层」，而不只是「是否改了 Skill」。"""
    assert _touched_skill_scopes(
        "sandbox_write_file", {"path": "skills/custom/a/SKILL.md"}
    ) == {SCOPE_USER}
    assert _touched_skill_scopes("sandbox_delete_path", {"path": "skills/custom/a"}) == {
        SCOPE_USER
    }
    assert _touched_skill_scopes(
        "sandbox_move_path", {"from": "skills/custom/a", "to": "skills/custom/b"}
    ) == {SCOPE_USER}
    assert _touched_skill_scopes(
        "sandbox_run_command", {"command": "python skills/custom/mk.py"}
    ) == {SCOPE_USER}
    assert _touched_skill_scopes(
        "sandbox_run_command", {"command": "python -m skill-creator"}
    ) == {SCOPE_USER}
    # 项目级路径：只命中项目层，不该顺带把用户级也拉一遍
    assert _touched_skill_scopes(
        "sandbox_write_file", {"path": "skills/project/ws-1/a/SKILL.md"}
    ) == {SCOPE_PROJECT}
    # 两边都提到（脚本同时写两层）：两层都拉
    assert _touched_skill_scopes(
        "sandbox_run_command",
        {"command": "python tool.py --from skills/custom --to skills/project/ws-1"},
    ) == {SCOPE_USER, SCOPE_PROJECT}
    # 不相关：路径在工作区、命令里没有 skills 字样（这类要靠挂起回拉兜底）
    assert not _touched_skill_scopes(
        "sandbox_write_file", {"path": "workspace/w1/main.py"}
    )
    assert not _touched_skill_scopes("sandbox_run_command", {"command": "python build.py"})
    # 前缀必须按目录段匹配：`skills/custom2` 不是用户级 Skill 目录
    assert not _touched_skill_scopes(
        "sandbox_write_file", {"path": "skills/custom2/a/SKILL.md"}
    )
