"""B1-2：归属校验（7.5）、D9 更新事务（7.4）、同步前四类校验（7.6）。

这一层的验收不是「功能跑通」，而是**失败时会发生什么**：

- 无权访问 → 一个目录都不创建，读返回 `unavailable`（不是空索引）；
- 候选内容坏掉 → 权威副本逐字节不变，旧版本继续服务，候选留在 `.rejected/`；
- 候选不完整（单文件失败 / 沙箱返回空树）→ 丢弃候选，不用残缺内容覆盖；
- 替换中途崩溃 → 下次访问自动回滚，不丢 Skill；
- 暂存目录绝不外泄到沙箱（`_scan` 会跳过 "." 开头的目录，但**同步不受 `_scan` 约束**）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fake_skill_sandbox import FakeSkillSandbox, skill_md

from edith_backend.internal.skills.storage import (
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
)
from edith_backend.internal.skills.storage.access import (
    DENIED_REASON,
    UNCONFIGURED_REASON,
    SkillAccessDenied,
    assert_access,
    workspace_access_checker,
)
from edith_backend.internal.skills.storage.local import (
    _IGNORE_CONTENT,
    CONFLICT_DIR,
    LocalSkillBackend,
)
from edith_backend.internal.skills.storage.project import ProjectSkillBackend
from edith_backend.internal.skills.storage.transaction import (
    OLD_PREFIX,
    REJECTED_DIR,
    STAGING_PREFIX,
    commit,
    discard,
    new_staging,
    pending_recovery,
    purge_stale_staging,
    recover,
    reject,
)
from edith_backend.internal.skills.storage.validate import (
    KIND_COUNT,
    KIND_FORMAT,
    KIND_RESERVED,
    KIND_SIZE,
    validate_skill_tree,
)
from edith_backend.internal.workspace.sync import is_ignored, load_synced_paths

USER = "user_alice"
WS_A = "ws-alpha"


# --------------------------------------------------------------------------
# 一、归属校验：判定在存储层，且 fail-closed
# --------------------------------------------------------------------------
async def _deny_all(user_id: str, workspace_id: str) -> bool:
    return False


async def _allow_all(user_id: str, workspace_id: str) -> bool:
    return True


def _project(tmp_path, checker) -> ProjectSkillBackend:
    return ProjectSkillBackend(
        WS_A, root=str(tmp_path / "project-skills"), access_checker=checker
    )


def test_read_denied_reports_unavailable_and_creates_nothing(tmp_path) -> None:
    backend = _project(tmp_path, _deny_all)
    index = asyncio.run(backend.read_index(USER))

    assert index.status == STATUS_UNAVAILABLE
    assert index.reason == DENIED_REASON
    # 「先建再删」会留下空壳，让「有目录但读不到」变成悬案
    assert not backend.user_dir(USER).exists()


def test_push_and_pull_denied_create_nothing(tmp_path) -> None:
    backend = _project(tmp_path, _deny_all)
    sandbox = FakeSkillSandbox()

    pushed = asyncio.run(backend.push(USER, sandbox))
    pulled = asyncio.run(backend.pull(USER, sandbox))

    assert pushed.status == STATUS_UNAVAILABLE and pushed.reason == DENIED_REASON
    assert pulled.status == STATUS_UNAVAILABLE and pulled.reason == DENIED_REASON
    assert not backend.user_dir(USER).exists()
    assert sandbox.files.write_calls == []


def test_missing_checker_is_fail_closed(tmp_path) -> None:
    """没注入判定器 ≠ 放行。少一次配置不该变成「所有项目对所有用户可见」。"""
    backend = ProjectSkillBackend(WS_A, root=str(tmp_path / "project-skills"))
    index = asyncio.run(backend.read_index(USER))

    assert index.status == STATUS_UNAVAILABLE
    assert index.reason == UNCONFIGURED_REASON
    assert not backend.user_dir(USER).exists()


def test_allowed_checker_reads_normally(tmp_path) -> None:
    backend = _project(tmp_path, _allow_all)
    assert asyncio.run(backend.read_index(USER)).status != STATUS_UNAVAILABLE


def test_assert_access_denies_empty_ids() -> None:
    async def check(user_id: str, workspace_id: str) -> bool:
        return True

    with pytest.raises(SkillAccessDenied):
        asyncio.run(assert_access(check, "", WS_A))
    with pytest.raises(SkillAccessDenied):
        asyncio.run(assert_access(check, USER, ""))


class _FakeStore:
    """只实现 `exists`，模拟 `WorkspaceStore.exists` 的判据形态。"""

    def __init__(self, owned: set[tuple[str, str]]) -> None:
        self._owned = owned

    async def exists(self, user_id: str, workspace_id: str) -> bool:
        return (user_id, workspace_id) in self._owned


def test_workspace_checker_uses_store_existence() -> None:
    checker = workspace_access_checker(_FakeStore({(USER, WS_A)}))

    assert asyncio.run(checker(USER, WS_A)) is True
    # 不区分「不存在」与「不属于你」：两者都只能得到 False
    assert asyncio.run(checker(USER, "ws-other")) is False
    assert asyncio.run(checker("user_bob", WS_A)) is False


def test_user_scope_has_no_access_gate(tmp_path) -> None:
    """用户级的隔离维度就是 user_id 本身，不该被项目级的判定器拦住。"""
    backend = LocalSkillBackend(tmp_path / "user-skills")
    assert asyncio.run(backend.read_index(USER)).status != STATUS_UNAVAILABLE


# --------------------------------------------------------------------------
# 二、四类校验（7.6）：errors 拒绝整份，warnings 放行但上报
# --------------------------------------------------------------------------
def _tree(root, entries: dict[str, str]):
    for dir_name, content in entries.items():
        target = root / dir_name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(content, encoding="utf-8")
    return root


def test_validate_accepts_valid_tree(tmp_path) -> None:
    root = _tree(
        tmp_path,
        {
            "alpha": skill_md("alpha", "A"),
            "beta-two": skill_md("beta-two", "B"),
        },
    )
    result = validate_skill_tree(root)

    assert result.ok
    assert result.names == ["alpha", "beta-two"]
    assert not result.warnings


def test_validate_rejects_unclosed_frontmatter(tmp_path) -> None:
    root = _tree(tmp_path, {"alpha": "---\nname: alpha\n"})
    result = validate_skill_tree(root)

    assert not result.ok
    assert result.errors[0].kind == KIND_FORMAT
    assert "invalid SKILL.md" in result.errors[0].reason


def test_validate_rejects_dir_name_mismatch(tmp_path) -> None:
    """目录名必须等于 frontmatter name：B2 的 sandbox_path 由目录名拼出。"""
    root = _tree(tmp_path, {"alpha": skill_md("alpha-renamed", "A")})
    result = validate_skill_tree(root)

    assert not result.ok
    assert "must equal frontmatter name" in result.errors[0].reason


def test_validate_rejects_bad_name_pattern(tmp_path) -> None:
    root = _tree(tmp_path, {"Bad_Name": skill_md("Bad_Name", "A")})
    result = validate_skill_tree(root)

    assert not result.ok
    assert "must match" in result.errors[0].reason


def test_validate_rejects_reserved_name(tmp_path) -> None:
    root = _tree(tmp_path, {"skill-creator": skill_md("skill-creator", "仿冒")})
    result = validate_skill_tree(root, reserved={"skill-creator"})

    assert not result.ok
    assert result.errors[0].kind == KIND_RESERVED


def test_validate_rejects_oversized_skill_md(tmp_path) -> None:
    body = "x" * (33 * 1024)
    root = _tree(tmp_path, {"alpha": skill_md("alpha", "A") + body})
    result = validate_skill_tree(root)

    assert not result.ok
    assert result.errors[0].kind == KIND_SIZE


def test_validate_rejects_over_count(tmp_path) -> None:
    root = _tree(
        tmp_path,
        {name: skill_md(name, name) for name in ("a1", "a2", "a3")},
    )
    result = validate_skill_tree(root, max_count=2)

    assert not result.ok
    assert result.errors[-1].kind == KIND_COUNT


def test_validate_warns_on_dir_without_skill_md(tmp_path) -> None:
    """不是 Skill 的目录只告警：判死会让一个杂项目录阻断全部编辑。"""
    root = _tree(tmp_path, {"alpha": skill_md("alpha", "A")})
    (root / "notes").mkdir()

    result = validate_skill_tree(root)

    assert result.ok
    assert [w.skill for w in result.warnings] == ["notes"]


# --------------------------------------------------------------------------
# 三、替换事务（7.4）：崩溃可恢复、内部文件不动、被拒候选不消失
# --------------------------------------------------------------------------
def _skill(root, name: str, description: str = "d") -> None:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(skill_md(name, description), encoding="utf-8")


def test_commit_replaces_and_propagates_delete(tmp_path) -> None:
    live = tmp_path / "live"
    _skill(live, "keep", "old")
    _skill(live, "gone", "old")

    staging = new_staging(live)
    _skill(staging, "keep", "new")

    moved = commit(staging, live)

    assert moved == 1
    assert (live / "keep" / "SKILL.md").read_text() == skill_md("keep", "new")
    assert not (live / "gone").exists()
    assert not staging.exists()


def test_commit_leaves_internal_files_alone(tmp_path) -> None:
    live = tmp_path / "live"
    _skill(live, "demo")
    (live / CONFLICT_DIR / "20260101-000000" / "demo").mkdir(parents=True)
    (live / CONFLICT_DIR / "20260101-000000" / "demo" / "SKILL.md").write_text(
        "old", encoding="utf-8"
    )
    (live / REJECTED_DIR / "20260101-000000" / "demo").mkdir(parents=True)
    (live / "overview.md").write_text("index", encoding="utf-8")

    staging = new_staging(live)
    _skill(staging, "demo", "new")
    commit(staging, live)

    assert (live / "overview.md").read_text() == "index"
    assert (live / CONFLICT_DIR / "20260101-000000" / "demo" / "SKILL.md").exists()
    assert (live / REJECTED_DIR / "20260101-000000" / "demo").exists()
    # 只搬非 "." 开头的条目：暂存目录里的 .edithignore 不该覆盖正式的那份
    assert not any(p.name.startswith(OLD_PREFIX) for p in live.iterdir())


def test_recover_restores_when_target_missing(tmp_path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    old = live / f"{OLD_PREFIX}20260101-000000-abcdef"
    _skill(old, "demo")
    assert pending_recovery(live)

    restored = recover(live)

    assert restored == ["demo"]
    assert (live / "demo" / "SKILL.md").is_file()
    assert not old.exists()
    assert not pending_recovery(live)


def test_recover_drops_stale_old_when_target_present(tmp_path) -> None:
    live = tmp_path / "live"
    _skill(live, "demo")
    old = live / f"{OLD_PREFIX}20260101-000000-abcdef"
    _skill(old, "demo")

    assert recover(live) == []
    # 新版本已在位 → 旧残留必须清掉，否则下次恢复判不清哪份是权威
    assert not old.exists()
    assert (live / "demo" / "SKILL.md").is_file()


def test_reject_keeps_authoritative_and_stores_candidate(tmp_path) -> None:
    live = tmp_path / "live"
    _skill(live, "demo")
    staging = new_staging(live)
    (staging / "demo").mkdir()
    (staging / "demo" / "SKILL.md").write_text("broken", encoding="utf-8")

    relative = reject(staging, live)

    assert relative.startswith(REJECTED_DIR + "/")
    assert not staging.exists()
    assert (live / relative / "demo" / "SKILL.md").read_text() == "broken"
    assert (live / "demo" / "SKILL.md").read_text() == skill_md("demo", "d")


def test_discard_leaves_authoritative_untouched(tmp_path) -> None:
    live = tmp_path / "live"
    _skill(live, "demo")
    staging = new_staging(live)
    before = (live / "demo" / "SKILL.md").read_text()

    discard(staging)

    assert (live / "demo" / "SKILL.md").read_text() == before
    assert not staging.exists()


# --------------------------------------------------------------------------
# 清理不能反过来改变「数据有没有落地」这个结论
# --------------------------------------------------------------------------


def test_cleanup_survives_delete_guard_raising_system_exit(tmp_path, monkeypatch) -> None:
    """`shutil.rmtree(ignore_errors=True)` 吞不掉 SystemExit。

    环境注入的删除护栏、以及部分安全软件，都会在 rmtree 内部抛非 OSError，
    而 `ignore_errors` 只认 OSError —— 异常会从 asyncio 任务里穿出去终止事件循环。
    这条钉住：整树删失败必须退化成逐条删，而不是上抛。
    """
    live = tmp_path / "live"
    _skill(live, "demo")
    staging = new_staging(live)
    (staging / ".edithignore").write_text("x", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise SystemExit(1)

    monkeypatch.setattr("shutil.rmtree", boom)

    discard(staging)  # 不许抛

    assert not staging.exists()


def test_commit_succeeds_even_if_old_cleanup_is_blocked(tmp_path, monkeypatch) -> None:
    """收尾清不掉 `.old` 时，commit 依然是成功：数据已经换过去了。

    把它判成失败会谎报「没落地」，调用方会跳过基线推进、下一轮再拉一遍。
    """
    live = tmp_path / "live"
    _skill(live, "demo", "old")
    staging = new_staging(live)
    _skill(staging, "demo", "new")

    monkeypatch.setattr(
        "edith_backend.internal.skills.storage.transaction._remove_tree",
        lambda _path: False,
    )

    assert commit(staging, live) == 1
    assert (live / "demo" / "SKILL.md").read_text() == skill_md("demo", "new")


def test_recover_cleans_one_stale_old_even_if_another_is_blocked(
    tmp_path, monkeypatch
) -> None:
    """一个残留目录搬不回去，不能让同批其它残留也清不掉。

    否则「旧版本残留」会永久堆积，每次访问都要重新遍历一遍（`failed` 曾经是全局的）。
    """
    live = tmp_path / "live"
    live.mkdir()
    blocked = live / f"{OLD_PREFIX}20260101-000000-aaaaaa"
    _skill(blocked, "stuck")
    stale = live / f"{OLD_PREFIX}20260101-000001-bbbbbb"
    _skill(stale, "demo")
    _skill(live, "demo")  # 新版本已在位 → stale 是纯残留

    real_rename = Path.rename

    def flaky_rename(self, target):
        if self.name == "stuck":
            raise OSError("file is in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    assert recover(live) == []
    assert blocked.is_dir()  # 唯一副本，清不得
    assert not stale.exists()  # 另一份残留照样清掉


def test_stale_staging_purged_but_fresh_one_kept(tmp_path) -> None:
    """暂存目录按年龄清理：太老的是崩溃残留，刚建的可能来自并发 pull。"""
    live = tmp_path / "live"
    _skill(live, "demo")
    old = new_staging(live)
    (old / "SKILL.md").write_text("crashed", encoding="utf-8")
    fresh = new_staging(live)
    three_hours_ago = time.time() - 3 * 60 * 60
    os.utime(old, (three_hours_ago, three_hours_ago))

    assert purge_stale_staging(live) == 1
    assert not old.exists()
    assert fresh.exists()


# --------------------------------------------------------------------------
# 四、拉取的失败语义（端到端，走 LocalSkillBackend）
# --------------------------------------------------------------------------
def _backend(tmp_path) -> LocalSkillBackend:
    return LocalSkillBackend(tmp_path / "user-skills")


def _seed_local(tmp_path, name: str = "demo", description: str = "本地") -> str:
    backend = _backend(tmp_path)
    _skill(backend.user_dir(USER), name, description)
    return (backend.user_dir(USER) / name / "SKILL.md").read_text()


def test_pull_broken_skill_keeps_previous_version(tmp_path) -> None:
    before = _seed_local(tmp_path)
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()
    sandbox.put("demo/SKILL.md", "---\nname: demo\n")  # frontmatter 未闭合

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_PARTIAL
    assert "rejected" in result.reason
    # 逐字节未变：用户改坏一次不等于丢掉原来能用的版本
    assert (backend.user_dir(USER) / "demo" / "SKILL.md").read_text() == before
    assert result.rejected and result.rejected[0].startswith(REJECTED_DIR + "/")
    # 索引仍列出它 —— 坏候选从未生效
    assert [s.name for s in asyncio.run(backend.read_index(USER)).summaries] == ["demo"]


def test_pull_reserved_name_rejected(tmp_path) -> None:
    before = _seed_local(tmp_path, "demo", "本地")
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()
    # 仿冒平台保留名（目录名与 frontmatter 一致，确保拦在保留名而不是格式）
    sandbox.put("skill-creator/SKILL.md", skill_md("skill-creator", "仿冒"))

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_PARTIAL
    assert "reserved" in result.reason
    assert (backend.user_dir(USER) / "demo" / "SKILL.md").read_text() == before
    assert not (backend.user_dir(USER) / "skill-creator").exists()


def test_pull_empty_sandbox_tree_does_not_wipe(tmp_path) -> None:
    """沙箱返回空树（疑似重置）时替换会删光全部 Skill —— 直接拒绝。"""
    _seed_local(tmp_path)
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_PARTIAL
    assert "returned nothing" in result.reason
    assert (backend.user_dir(USER) / "demo" / "SKILL.md").is_file()


def test_pull_single_file_failure_keeps_authoritative(tmp_path) -> None:
    before = _seed_local(tmp_path)
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()
    sandbox.put("demo/SKILL.md", skill_md("demo", "新版本"))
    sandbox.put("demo/extra.md", "x")
    sandbox.files.read_error = OSError("boom")

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_PARTIAL
    assert "discarded" in result.reason
    assert (backend.user_dir(USER) / "demo" / "SKILL.md").read_text() == before
    assert not (backend.user_dir(USER) / "demo" / "extra.md").exists()


def test_pull_success_reports_real_change_and_deletes(tmp_path) -> None:
    _seed_local(tmp_path)  # demo
    backend = _backend(tmp_path)
    _skill(backend.user_dir(USER), "obsolete", "旧")
    sandbox = FakeSkillSandbox()
    # 删除语义的前提是「沙箱见过当前权威内容」：先推一次，再模拟用户在沙箱里删掉它。
    # 不先 push 的话，候选缺少 obsolete 只说明它没见过这个 Skill —— 那种情况必须
    # 压制删除（见 test_skill_storage_local::test_pull_does_not_delete_what_the_sandbox_never_saw）。
    asyncio.run(backend.push(USER, sandbox))
    sandbox.put("demo/SKILL.md", skill_md("demo", "改过"))
    sandbox.files.blobs.pop("/home/user/skills/custom/obsolete/SKILL.md")

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_OK
    assert result.pulled == 1
    assert result.deleted == 1
    assert not (backend.user_dir(USER) / "obsolete").exists()
    assert "改过" in (backend.user_dir(USER) / "demo" / "SKILL.md").read_text()


def test_pull_brings_binary_assets(tmp_path) -> None:
    """候选要能完整替代旧目录：二进制资源不回拉 → 替换会把它删掉。"""
    _seed_local(tmp_path)
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()
    sandbox.put("demo/SKILL.md", skill_md("demo", "本地"))
    sandbox.files.write_now("/home/user/skills/custom/demo/logo.png", b"\x89PNG\x00")

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_OK
    assert (backend.user_dir(USER) / "demo" / "logo.png").read_bytes() == b"\x89PNG\x00"


def test_pull_oversized_asset_keeps_authoritative(tmp_path) -> None:
    """单文件超限被跳过 → 候选里缺这个文件 → 替换会把它删掉，必须拒绝。"""
    before = _seed_local(tmp_path)
    backend = _backend(tmp_path)
    sandbox = FakeSkillSandbox()
    sandbox.put("demo/SKILL.md", skill_md("demo", "新版本"))
    sandbox.files.write_now(
        "/home/user/skills/custom/demo/big.bin", b"x" * (3 * 1024 * 1024)
    )

    result = asyncio.run(backend.pull(USER, sandbox))

    assert result.status == STATUS_PARTIAL
    assert "exceed" in result.reason
    assert (backend.user_dir(USER) / "demo" / "SKILL.md").read_text() == before


def test_pull_failure_restores_consensus_state(tmp_path) -> None:
    """候选被丢弃时共识集合必须还原，否则下次删除传播会基于残缺集合误删。"""
    _seed_local(tmp_path)
    backend = _backend(tmp_path)
    key = backend.state_key(USER)
    sandbox = FakeSkillSandbox()
    sandbox.put("demo/SKILL.md", skill_md("demo", "x"))
    sandbox.put("other/SKILL.md", skill_md("other", "x"))
    sandbox.files.read_error = OSError("boom")

    before = load_synced_paths(key)
    asyncio.run(backend.pull(USER, sandbox))
    after = load_synced_paths(key)

    assert after == before


def test_ensure_recovers_interrupted_replace(tmp_path) -> None:
    backend = _backend(tmp_path)
    directory = backend.user_dir(USER)
    directory.mkdir(parents=True)
    old = directory / f"{OLD_PREFIX}20260101-000000-abcdef"
    _skill(old, "demo")

    asyncio.run(backend.ensure(USER))

    assert (directory / "demo" / "SKILL.md").is_file()
    assert not old.exists()


# --------------------------------------------------------------------------
# 五、暂存目录不外泄（`_scan` 跳过 "." 开头，但同步不受 `_scan` 约束）
# --------------------------------------------------------------------------
def test_ignore_list_covers_transaction_dirs() -> None:
    patterns = [line for line in _IGNORE_CONTENT.splitlines() if line]
    for rel in (
        f"{STAGING_PREFIX}20260101-000000-abcdef/demo/SKILL.md",
        f"{OLD_PREFIX}20260101-000000-abcdef/demo/SKILL.md",
        f"{REJECTED_DIR}/20260101-000000-abcdef/demo/SKILL.md",
    ):
        assert is_ignored(rel, patterns), rel
    assert not is_ignored("demo/SKILL.md", patterns)


def test_push_does_not_leak_staging_dir(tmp_path) -> None:
    _seed_local(tmp_path)
    backend = _backend(tmp_path)
    staging = new_staging(backend.user_dir(USER))
    _skill(staging, "half-written")
    sandbox = FakeSkillSandbox()

    asyncio.run(backend.push(USER, sandbox))

    leaked = [rel for rel in sandbox.files.tree() if rel.startswith(STAGING_PREFIX)]
    assert leaked == []
    assert sandbox.has("demo/SKILL.md")
