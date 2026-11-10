"""存量 Skill 显式迁移（5-A / 5-B，设计稿 9.1）的测试。

覆盖的是**协议**而不是 happy path：

| 断言 | 对应条款 |
| --- | --- |
| 未知字段 / 缺字段 / 空列表 → 退出码 2 且一个文件都不写 | 9.1 阶段 0 |
| 批内重复目标 → 整批拒绝 | 9.1 阶段 1 ⑥ |
| 外部 workspace、保留名、源无效 → 整批拒绝，**不是**逐条失败 | 9.1 阶段 1 ①③④⑤ |
| 目标已存在 → `skipped`，且**不覆盖** | 5-A 硬约束 |
| 重跑同一批 → 全 `skipped`、退出码 0 | 9.1「可重复执行」 |
| copy 后内容不一致 → `failed`，源保留、目标不落盘 | 9.1 阶段 2 |
| 单条执行失败不影响后续条目 | 9.1 阶段 3 |
| dry-run 零写入 | 5-A 默认 dry-run |

另有一条「放宽但不漏」的断言：`users` 表没有记录但磁盘上有该用户的 Skill 目录时
**允许**迁移（工具存在的意义就是处理库与磁盘不同步的存量数据），而拼错的 user_id
既没有行也没有目录，仍然失败。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest
from fake_skill_sandbox import skill_md

from edith_backend.internal.skills import migrate as migration
from edith_backend.internal.skills.migrate import (
    ACTION_FAILED,
    ACTION_MIGRATED,
    ACTION_SKIPPED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_REJECTED,
    Context,
    Item,
    MigrationError,
    _run,
    load_batch,
    migrate_one,
    parse_target,
    precheck,
)
from edith_backend.internal.skills.storage.access import workspace_access_checker
from edith_backend.internal.skills.storage.local import LocalSkillBackend
from edith_backend.internal.skills.storage.validate import (
    SKILL_COUNT_MAX,
    platform_skill_names,
)
from edith_backend.internal.userconfig import schema as user_schema
from edith_backend.internal.workspace import schema as workspace_schema
from edith_backend.internal.workspace.store import WorkspaceStore

USER = "user_migrate"
OTHER = "user_other"
WS = "ws-migrate"


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------
@dataclass
class Env:
    """一次迁移的完整环境：库（users + workspaces）、用户级根、项目级根。"""

    db: aiosqlite.Connection
    ctx: Context
    user_root: Path
    project_root: Path

    async def close(self) -> None:
        await self.db.close()

    def user_skill_dir(self, name: str, dir_name: str | None = None) -> Path:
        target = self.user_root / USER / (dir_name or name)
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(skill_md(name, "演示用 Skill"), encoding="utf-8")
        return target

    def project_dir(self, user: str = USER, workspace: str = WS) -> Path:
        return self.project_root / user / workspace


async def make_env(
    tmp_path: Path,
    *,
    users: tuple[str, ...] = (USER,),
    workspaces: tuple[tuple[str, str], ...] = ((WS, USER),),
) -> Env:
    db = await aiosqlite.connect(tmp_path / "edith.db")
    await user_schema.create_schema(db)
    await workspace_schema.create_schema(db)
    for user_id in users:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    for workspace_id, owner in workspaces:
        await db.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name) VALUES (?, ?, ?)",
            (workspace_id, owner, workspace_id),
        )
    await db.commit()
    user_root = tmp_path / "user-skills"
    project_root = tmp_path / "project-skills"
    ctx = Context(
        db=db,
        checker=workspace_access_checker(WorkspaceStore(db)),
        user_backend=LocalSkillBackend(user_root),
        project_root=str(project_root),
    )
    return Env(db=db, ctx=ctx, user_root=user_root, project_root=project_root)


def item(index: int = 0, skill: str = "demo", to: str = f"project:{WS}") -> Item:
    return Item(index, USER, skill, parse_target(to))


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def test_target_parsing() -> None:
    assert parse_target("project:ws-1") == parse_target(" project:ws-1 ")
    assert parse_target("user").scope == "user"
    for bad in ("project:", "session:x", "", "PROJECT:ws"):
        with pytest.raises(MigrationError):
            parse_target(bad)


def test_batch_schema_is_strict(tmp_path: Path) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(
        "migrations:\n  - user: u1\n    skill: demo\n    to: project:ws1\n",
        encoding="utf-8",
    )
    items = load_batch(good)
    assert [(i.user, i.skill, i.target.workspace_id) for i in items] == [
        ("u1", "demo", "ws1")
    ]

    cases = {
        "unknown_top": "job:\n  - user: u1\n",
        "unknown_field": "migrations:\n  - user: u1\n    skill: demo\n    to: user\n    force: true\n",
        "missing_field": "migrations:\n  - user: u1\n    skill: demo\n",
        "empty": "migrations: []\n",
        "not_list": "migrations: {user: u1}\n",
        "bad_target": "migrations:\n  - user: u1\n    skill: demo\n    to: session:x\n",
    }
    for name, text in cases.items():
        path = tmp_path / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(MigrationError):
            load_batch(path)


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    async def go() -> int:
        env = await make_env(tmp_path)
        try:
            source = env.user_skill_dir("demo")
            before = {p.name for p in source.parent.iterdir()}
            code = await _run([item()], env.ctx, apply=False)
            assert source.is_dir()
            assert {p.name for p in source.parent.iterdir()} == before
            # 项目级根目录**整个不存在**：dry-run 连目录都不建
            assert not env.project_root.exists()
            return code
        finally:
            await env.close()

    assert asyncio.run(go()) == EXIT_OK
    assert "dry-run" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 迁移成功 / 幂等 / 不覆盖
# --------------------------------------------------------------------------
def test_apply_copies_keeps_source_and_refreshes_index(tmp_path: Path) -> None:
    async def go() -> None:
        env = await make_env(tmp_path)
        try:
            source = env.user_skill_dir("demo")
            (source / "references").mkdir()
            (source / "references" / "notes.md").write_text("参考内容", encoding="utf-8")
            before = {
                p.relative_to(source).as_posix(): p.read_bytes()
                for p in source.rglob("*")
                if p.is_file()
            }

            outcome = await migrate_one(env.ctx, item())
            assert outcome.action == ACTION_MIGRATED, outcome.reason

            dest = env.project_dir() / "demo"
            after = {
                p.relative_to(dest).as_posix(): p.read_bytes()
                for p in dest.rglob("*")
                if p.is_file()
            }
            assert after == before
            # 源永不自动删
            assert source.is_dir()

            overview = (env.project_dir() / "overview.md").read_text(encoding="utf-8")
            assert f"skills/project/{WS}/demo/SKILL.md" in overview
            # 暂存目录不留痕
            assert not list(env.project_dir().glob(".staging-*"))
        finally:
            await env.close()

    asyncio.run(go())


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    async def go() -> int:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("demo")
            assert await _run([item()], env.ctx, apply=True) == EXIT_OK
            # 重跑同一批：目标已存在 → skipped，退出码仍是 0（否则「确认全部到位」
            # 永远返回失败）
            return await _run([item()], env.ctx, apply=True)
        finally:
            await env.close()

    assert asyncio.run(go()) == EXIT_OK


def test_existing_target_is_skipped_and_not_overwritten(tmp_path: Path) -> None:
    async def go() -> None:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("demo")
            dest = env.project_dir() / "demo"
            dest.mkdir(parents=True)
            marker = dest / "SKILL.md"
            marker.write_text("已有的项目级版本", encoding="utf-8")

            outcome = await migrate_one(env.ctx, item())
            assert outcome.action == ACTION_SKIPPED
            assert marker.read_text(encoding="utf-8") == "已有的项目级版本"
        finally:
            await env.close()

    asyncio.run(go())


# --------------------------------------------------------------------------
# 整批预检查
# --------------------------------------------------------------------------
def test_duplicate_targets_reject_whole_batch(tmp_path: Path) -> None:
    async def go() -> tuple[int, bool]:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("demo")
            items = [item(0), item(1)]  # 完全相同的目标
            code = await _run(items, env.ctx, apply=True)
            return code, env.project_root.exists()
        finally:
            await env.close()

    code, project_exists = asyncio.run(go())
    assert code == EXIT_REJECTED
    assert project_exists is False


def test_foreign_workspace_rejects_whole_batch(tmp_path: Path) -> None:
    async def go() -> tuple[int, bool]:
        env = await make_env(tmp_path)  # WS 属于 USER，用 OTHER 去迁
        try:
            other = env.user_root / OTHER / "demo"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text(skill_md("demo", "x"), encoding="utf-8")
            items = [Item(0, OTHER, "demo", parse_target(f"project:{WS}"))]
            code = await _run(items, env.ctx, apply=True)
            return code, env.project_root.exists()
        finally:
            await env.close()

    code, project_exists = asyncio.run(go())
    assert code == EXIT_REJECTED
    assert project_exists is False


def test_reserved_name_rejects_whole_batch(tmp_path: Path) -> None:
    async def go() -> int:
        env = await make_env(tmp_path)
        try:
            reserved = min(platform_skill_names())
            # 目录名与 frontmatter name 一致，但撞平台保留名
            env.user_skill_dir(reserved)
            return await _run([item(skill=reserved)], env.ctx, apply=True)
        finally:
            await env.close()

    assert asyncio.run(go()) == EXIT_REJECTED


def test_precheck_lists_every_hard_error(tmp_path: Path) -> None:
    async def go() -> list[str]:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("ok-one")
            items = [
                item(0, "missing"),          # 源不存在
                item(1, "ok-one"),           # 正常
                Item(2, USER, "ghost", parse_target("project:not-mine")),  # 无权
            ]
            return await precheck(env.ctx, items)
        finally:
            await env.close()

    errors = asyncio.run(go())
    assert len(errors) == 2
    assert any("missing" in line for line in errors)
    assert any("workspace not accessible" in line for line in errors)


def test_to_user_is_skipped_not_failed(tmp_path: Path) -> None:
    async def go() -> int:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("demo")
            # 「确认留在用户级」是正常用法：预检查通过，执行记 skipped
            return await _run([item(to="user")], env.ctx, apply=True)
        finally:
            await env.close()

    assert asyncio.run(go()) == EXIT_OK


def test_full_scope_rejects_before_writing(tmp_path: Path) -> None:
    """目标作用域已满时**在写之前**拒绝：写完再判等于先污染再拒绝。"""

    async def go() -> tuple[str, int]:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("demo")
            dest_root = env.project_dir()
            dest_root.mkdir(parents=True)
            for i in range(SKILL_COUNT_MAX):
                name = f"fill-{i:03d}"
                (dest_root / name).mkdir()
                (dest_root / name / "SKILL.md").write_text(
                    skill_md(name, "占位"), encoding="utf-8"
                )
            check = await migration._check(env.ctx, item())
            return check.state, len(list(dest_root.iterdir()))
        finally:
            await env.close()

    state, count = asyncio.run(go())
    assert state == ACTION_FAILED
    assert count == SKILL_COUNT_MAX  # 一个都没多写


# --------------------------------------------------------------------------
# 执行期失败：逐条记录、继续、不作废已成功的条目
# --------------------------------------------------------------------------
def test_copy_failure_is_recorded_and_batch_continues(tmp_path: Path, monkeypatch) -> None:
    async def go() -> tuple[int, str, bool]:
        env = await make_env(tmp_path)
        try:
            env.user_skill_dir("bad")
            env.user_skill_dir("good")
            real = shutil.copytree

            def flaky(src, dst, *args, **kwargs):
                if Path(src).name == "bad":
                    raise OSError("模拟磁盘错误")
                return real(src, dst, *args, **kwargs)

            monkeypatch.setattr(migration.shutil, "copytree", flaky)
            items = [item(0, "bad"), item(1, "good")]
            code = await _run(items, env.ctx, apply=True)
            return code, "good", (env.project_dir() / "good").is_dir()
        finally:
            await env.close()

    code, _, good_written = asyncio.run(go())
    assert code == EXIT_PARTIAL  # 有 failed
    assert good_written is True  # 后续条目照常执行


def test_content_mismatch_aborts_that_item(tmp_path: Path, monkeypatch) -> None:
    """copy 之后内容不一致：该条 failed，源保留，目标不落盘。"""

    async def go() -> tuple[str, str, bool, bool]:
        env = await make_env(tmp_path)
        try:
            source = env.user_skill_dir("demo")
            real = shutil.copytree

            def tampered(src, dst, *args, **kwargs):
                result = real(src, dst, *args, **kwargs)
                (Path(dst) / "SKILL.md").write_text(
                    skill_md("demo", "被改过的描述"), encoding="utf-8"
                )
                return result

            monkeypatch.setattr(migration.shutil, "copytree", tampered)
            outcome = await migrate_one(env.ctx, item())
            return (
                outcome.action,
                outcome.reason,
                source.is_dir(),
                (env.project_dir() / "demo").exists(),
            )
        finally:
            await env.close()

    action, reason, source_kept, dest_written = asyncio.run(go())
    assert action == ACTION_FAILED
    assert "校验不一致" in reason
    assert source_kept is True
    assert dest_written is False


# --------------------------------------------------------------------------
# 用户存在性：放宽到「磁盘上有目录」，但不漏掉打错字
# --------------------------------------------------------------------------
def test_user_without_row_but_with_dir_is_allowed(tmp_path: Path) -> None:
    async def go() -> str:
        env = await make_env(tmp_path, users=())  # 库是空的（users 表存在但没这行）
        try:
            env.user_skill_dir("demo")
            outcome = await migrate_one(env.ctx, item())
            return outcome.action
        finally:
            await env.close()

    assert asyncio.run(go()) == ACTION_MIGRATED


def test_unknown_user_is_rejected(tmp_path: Path) -> None:
    async def go() -> int:
        env = await make_env(tmp_path, users=())
        try:
            # 既没有 users 行，也没有目录：这就是拼错字的样子
            return await _run([item()], env.ctx, apply=True)
        finally:
            await env.close()

    assert asyncio.run(go()) == EXIT_REJECTED


# --------------------------------------------------------------------------
# 入口：不带 --db 时必须自己解析库路径
# --------------------------------------------------------------------------
def test_main_resolves_db_path_without_flag(tmp_path: Path, monkeypatch) -> None:
    """不带 `--db` 时由 CLI 自己解析路径（曾经的 ImportError 会让整条命令直接崩）。

    这条断言只钉两个事实，不重复测参数校验：`.env` 被加载了、库路径走的是
    `config.database_path()`。缺少 `--user` 仍然以退出码 2 结束，说明崩点在
    「取配置」之前已经被修掉——若再写回不存在的符号，这里会抛 ImportError 而不是
    SystemExit。调用方（人）唯一的恢复手段就是显式 `--db`，而不该需要知道这个细节。
    """
    from edith_backend import config

    called: dict[str, object] = {}
    monkeypatch.setattr(
        config, "load_environment", lambda: called.setdefault("env", True)
    )
    monkeypatch.setattr(
        config, "database_path", lambda: str(tmp_path / "from-config.db")
    )
    monkeypatch.setattr(sys, "argv", ["edith-skill-migrate"])

    with pytest.raises(SystemExit) as exc:
        migration.main()

    assert exc.value.code == EXIT_REJECTED  # 缺必填参数，不是 ImportError
    assert called.get("env") is True
