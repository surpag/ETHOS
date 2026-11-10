"""存量 Skill 的显式迁移（设计稿第九节 / 9.1 / D11 的 5-A + 5-B）。

为什么是「工具」而不是「服务端自动归类」
--------------------------------------
任何「发现 `user-skills/{user_id}/x` 像项目 Skill 就搬走」的启发式都是**不可逆
的数据搬动**，判错的代价是用户的 Skill 静默换作用域（从此只在一个项目里可见，
或反过来对所有项目可见）。所以迁移只能由人显式发起：

    edith-skill-migrate --user U --skill NAME --to project:WS      # 5-A 单条
    edith-skill-migrate --file migration.yaml                      # 5-B 批量

三条不可让步的性质
----------------
1. **默认 dry-run**：不加 `--apply` 只打印将发生什么，一个文件都不写。
2. **源永不自动删除**：迁移是 copy，不是 move。删源是人的决定，不是工具的副作用。
3. **不做整体回滚**：自动回滚等于「删掉已成功的目标副本」，与「源不自动删」对称地
   危险。责任边界是逐条记录 + **可重复执行**：重跑同一份 YAML，已迁的条会因
   「目标已存在」变成 `skipped`，天然幂等。

预检查与执行必须共用同一套判定
----------------------------
`_check()` 是唯一的判定函数，预检查与执行各调一次。执行前重查不是多余：预检查到
逐条执行之间隔着一个窗口，期间 Skill 可能被 Agent 改掉、workspace 可能被删。
预检查是「快速失败」，**不是授权凭证**。共用函数还保证了两个阶段不会漂移
（两处各写一份规则，必然有一处漏改）。

逐字节校验，不按大小判等
----------------------
`size` 相等是「内容已变」最典型的样子（KB 级 markdown 改一个词，字节数常常不变），
所以 copy 之后按「相对路径 → sha256」整棵树比对，与用户 Skill 同步的
`verify_content=True` 同一个理由。

写入粒度是「一个 Skill 一个目录」
--------------------------------
候选先落 `.staging-<ts>/<name>`（该前缀已在 `.edithignore` 里，不会被扫成 Skill、
也不会被同步进沙箱），校验通过后整目录 rename 到位。逐文件写会留下「一半新一半旧」
的中间态，而 Skill 恰恰是「少一个 `---` 就整个失效」的东西。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import yaml

from ..workspace.store import WorkspaceStore
from .storage import transaction
from .storage.access import (
    AccessChecker,
    SkillAccessDenied,
    assert_access,
    workspace_access_checker,
)
from .storage.base import STATUS_UNAVAILABLE
from .storage.local import LocalSkillBackend
from .storage.project import ProjectSkillBackend
from .storage.validate import (
    KIND_COUNT,
    SKILL_COUNT_MAX,
    platform_skill_names,
    validate_skill_tree,
)
from .types import SCOPE_PROJECT, SCOPE_USER

EXIT_OK = 0
#: 有 failed 的条目（其余可能已迁移）
EXIT_PARTIAL = 1
#: schema / 预检查失败：**整批未执行**
EXIT_REJECTED = 2

ACTION_MIGRATED = "migrated"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"
#: 判定函数的内部状态：可以迁，但还没迁
READY = "ready"

#: 批量文件的顶层字段；多一个都报错（拼错字段名静默失效是最难查的一类）
_TOP_FIELDS = frozenset({"migrations"})
#: 每条记录的字段
_ITEM_FIELDS = frozenset({"user", "skill", "to"})


class MigrationError(Exception):
    """schema 或参数问题：整批不执行，退出码 2。"""


@dataclass(frozen=True)
class Target:
    scope: str
    workspace_id: str = ""

    @property
    def label(self) -> str:
        return "user" if self.scope == SCOPE_USER else f"project:{self.workspace_id}"


@dataclass(frozen=True)
class Item:
    index: int
    user: str
    skill: str
    target: Target

    def describe(self) -> str:
        return f"{self.user}/{self.skill} -> {self.target.label}"


@dataclass(frozen=True)
class Check:
    """一次判定：`ready` 可迁、`skipped` 无害跳过、`failed` 有问题。

    `skipped` 与 `failed` 刻意分开：目标已存在 / 本来就在用户级是**正常**结果
    （幂等重跑必然遇到），把它记成 failed 会让「重跑一次确认全部到位」变成一个
    永远返回退出码 1 的动作。
    """

    state: str
    reason: str = ""


@dataclass(frozen=True)
class Outcome:
    item: Item
    action: str
    reason: str = ""

    def render(self) -> str:
        suffix = f"  ({self.reason})" if self.reason else ""
        return f"[{self.item.index}] {self.action:<9} {self.item.describe()}{suffix}"


@dataclass
class Context:
    db: aiosqlite.Connection
    checker: AccessChecker
    user_backend: LocalSkillBackend
    project_root: str | None = None

    def project(self, workspace_id: str) -> ProjectSkillBackend:
        return ProjectSkillBackend(
            workspace_id, self.project_root, access_checker=self.checker
        )


# ---------------------------------------------------------------- 解析


def parse_target(raw: object) -> Target:
    """`project:<workspace_id>` 或 `user`；其余一律拒绝。"""
    text = str(raw or "").strip()
    if text == SCOPE_USER:
        return Target(SCOPE_USER)
    if text.startswith(f"{SCOPE_PROJECT}:"):
        workspace = text[len(SCOPE_PROJECT) + 1 :].strip()
        if not workspace:
            raise MigrationError("target 'project:' 缺少 workspace id")
        return Target(SCOPE_PROJECT, workspace)
    raise MigrationError(f"未知目标 {raw!r}：应为 'project:<workspace_id>' 或 'user'")


def load_batch(path: Path) -> list[Item]:
    """读 `migration.yaml`。未知字段**报错**而不是忽略。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MigrationError(f"读不到迁移文件 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise MigrationError(f"YAML 解析失败 {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise MigrationError("迁移文件顶层必须是映射，且含 `migrations` 列表")
    unknown = set(data) - _TOP_FIELDS
    if unknown:
        raise MigrationError(f"未知顶层字段：{', '.join(sorted(unknown))}")

    rows = data.get("migrations")
    if not isinstance(rows, list) or not rows:
        raise MigrationError("`migrations` 必须是非空列表")

    items: list[Item] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MigrationError(f"[{index}] 每条必须是映射")
        missing = _ITEM_FIELDS - set(row)
        if missing:
            raise MigrationError(f"[{index}] 缺少字段：{', '.join(sorted(missing))}")
        unknown = set(row) - _ITEM_FIELDS
        if unknown:
            raise MigrationError(f"[{index}] 未知字段：{', '.join(sorted(unknown))}")
        user = str(row["user"] or "").strip()
        skill = str(row["skill"] or "").strip()
        if not user or not skill:
            raise MigrationError(f"[{index}] user / skill 不能为空")
        items.append(Item(index, user, skill, parse_target(row["to"])))
    return items


# ---------------------------------------------------------------- 判定


def _digest(root: Path) -> dict[str, str]:
    """相对路径（posix）→ sha256。"""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        found[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return found


def _inspect_skill(parent: Path, dir_name: str) -> list[str]:
    """只取与这个目录相关的校验结论（规则复用同步前校验，不另写一份）。

    目录名 ≠ frontmatter name、保留名、体积上限这几条一旦两处实现，迟早有一处
    漏改；而漏改的表现是「迁移成功但永远读不到」——它不报错。
    """
    result = validate_skill_tree(parent, reserved=platform_skill_names())
    reasons = [issue.reason for issue in result.errors if issue.skill == dir_name]
    if dir_name not in result.names and not reasons:
        reasons = [
            issue.reason for issue in result.warnings if issue.skill == dir_name
        ] or ["not a valid skill"]
    return reasons


def _count_limit_error(directory: Path) -> str:
    """目标作用域是否放得下**再多一个**（写完再判等于先污染再拒绝）。

    `validate_skill_tree` 只在「已超过上限」时报警，而这里要问的是「再加一个会不会
    超」，所以必须在它的结论之外自己加 1——否则上限恰好卡在 100 时，第 101 个会被
    放进去，然后才在别的路径上被判死（本次迁移就成了那次污染）。
    """
    if not directory.is_dir():
        return ""
    result = validate_skill_tree(directory)
    for issue in result.errors:
        if issue.kind == KIND_COUNT:
            return issue.reason
    if len(result.names) + 1 > SKILL_COUNT_MAX:
        return f"{len(result.names)} skills exceed limit {SKILL_COUNT_MAX}"
    return ""


async def _user_known(ctx: Context, user: str) -> tuple[bool, str]:
    """用户是否可解析：`users` 里有行，**或**磁盘上已有一份权威副本目录。

    放宽到「目录存在也算」是刻意的：这个工具存在的意义正是处理库与磁盘不同步的
    存量数据（库重建过、Skill 由运维直接拷入）。严格要求 `users` 行会让工具在最
    需要它的时候恰好不可用。而拼错的 user_id 不可能凭空产生目录，所以放宽不会把
    「打错字」漏过去——打错字的 user_id 既没有行也没有目录，仍然失败。
    """
    try:
        async with ctx.db.execute(
            "SELECT 1 FROM users WHERE user_id = ? LIMIT 1", (user,)
        ) as cursor:
            if await cursor.fetchone() is not None:
                return True, ""
    except sqlite3.Error:
        # 表还没建（空库）：退回磁盘判定，别把「库是新的」当成「用户不存在」
        pass
    if ctx.user_backend.user_dir(user).is_dir():
        return True, "user 不在 users 表，按已有 Skill 目录继续"
    return False, "user 不存在（users 表无记录且没有 Skill 目录）"


async def _check(ctx: Context, item: Item) -> Check:
    """唯一的判定函数：预检查与执行前重查都走这里。"""
    known, note = await _user_known(ctx, item.user)
    if not known:
        return Check(ACTION_FAILED, note)
    if item.target.scope == SCOPE_USER:
        # 源本来就是用户级：这是一条「确认留在用户级」的记录，不是错误
        return Check(ACTION_SKIPPED, "already at user scope")
    try:
        await assert_access(ctx.checker, item.user, item.target.workspace_id)
    except SkillAccessDenied as exc:
        return Check(ACTION_FAILED, exc.reason)

    source_parent = ctx.user_backend.user_dir(item.user)
    source = source_parent / item.skill
    if not source.is_dir():
        return Check(ACTION_FAILED, "源 Skill 不存在")
    problems = _inspect_skill(source_parent, item.skill)
    if problems:
        return Check(ACTION_FAILED, f"源 Skill 无效：{problems[0]}")

    project = ctx.project(item.target.workspace_id)
    dest_root = project.user_dir(item.user)
    if (dest_root / item.skill).exists():
        return Check(ACTION_SKIPPED, "目标已存在")
    over = _count_limit_error(dest_root)
    if over:
        return Check(ACTION_FAILED, f"目标作用域已满：{over}")
    return Check(READY, note)


# ---------------------------------------------------------------- 执行


async def _refresh_index(project: ProjectSkillBackend, user: str) -> str:
    """迁完立刻重建目标作用域索引，返回需要展示的告警（无则空串）。

    索引是派生缓存，下一个 run 读它时本来也会自愈；主动刷一次是为了让人**当场**
    核对结果。刷新失败不回滚迁移——文件已经就位，自愈会补上。
    """
    index = await project.read_index(user)
    if index.status == STATUS_UNAVAILABLE:
        return f"warning: 索引刷新失败（下次读取会自愈）：{index.reason}"
    return ""


async def migrate_one(ctx: Context, item: Item) -> Outcome:
    """执行一条：重查 → 暂存 copy → 逐字节校验 → 目录级 rename → 刷索引。"""
    check = await _check(ctx, item)
    if check.state != READY:
        return Outcome(item, check.state, check.reason)

    project = ctx.project(item.target.workspace_id)
    try:
        # ensure 负责建根目录 + 崩溃恢复 + 写 .edithignore；它内部的 assert_access
        # 会把「预检查之后被收回的权限」挡在这里
        await project.ensure(item.user)
    except SkillAccessDenied as exc:
        return Outcome(item, ACTION_FAILED, exc.reason)
    except (OSError, ValueError) as exc:
        return Outcome(item, ACTION_FAILED, f"准备目标目录失败：{exc}")

    source = ctx.user_backend.user_dir(item.user) / item.skill
    dest_root = project.user_dir(item.user)
    dest = dest_root / item.skill
    staging = transaction.new_staging(dest_root)
    candidate = staging / item.skill
    try:
        shutil.copytree(source, candidate)
        expected = _digest(source)
        if _digest(candidate) != expected:
            return Outcome(item, ACTION_FAILED, "复制校验不一致（源保留）")
        if dest.exists():
            # 预检查到此刻之间目标出现了：不覆盖（5-A 硬约束）
            return Outcome(item, ACTION_SKIPPED, "迁移过程中目标出现")
        candidate.rename(dest)
    except OSError as exc:
        return Outcome(item, ACTION_FAILED, f"复制失败：{exc}")
    finally:
        transaction.discard(staging)

    if _digest(dest) != expected:
        # rename 是原子的，走到这里说明文件系统出了问题。**不回滚**：目标目录已
        # 存在，删掉它就是删掉一份可能完整的内容（与「源不自动删」同一条原则）
        return Outcome(item, ACTION_FAILED, "移动后校验不一致（目标保留，需人工核对）")

    warning = await _refresh_index(project, item.user)
    return Outcome(item, ACTION_MIGRATED, warning)


async def precheck(ctx: Context, items: list[Item]) -> list[str]:
    """整批预检查：一次列出全部硬问题，任一存在则整批不执行。

    为什么整批而不是逐条：批量迁移的价值是「一次判断这一批对不对」。逐条跑到
    一半再报错，运维面对的是「一半已迁、一半拒绝」的中间态，而这里没有回滚，
    只能人肉比对。批内重复也必须整批拒绝——两条写到同一目标时，结果由顺序决定。
    """
    errors: list[str] = []
    seen: dict[tuple[str, str, str, str], int] = {}
    for item in items:
        key = (item.user, item.target.scope, item.target.workspace_id, item.skill)
        if key in seen:
            errors.append(
                f"[{item.index}] 目标重复 {item.describe()}（与 [{seen[key]}] 相同）"
            )
            continue
        seen[key] = item.index
    for item in items:
        check = await _check(ctx, item)
        if check.state == ACTION_FAILED:
            errors.append(f"[{item.index}] {item.describe()}：{check.reason}")
    return errors


async def _run(items: list[Item], ctx: Context, *, apply: bool) -> int:
    errors = await precheck(ctx, items)
    if errors:
        print("预检查失败，整批未执行：", file=sys.stderr)
        for line in errors:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_REJECTED

    if not apply:
        for item in items:
            check = await _check(ctx, item)
            action = "migrate" if check.state == READY else "skip"
            suffix = f"  ({check.reason})" if check.reason else ""
            print(f"[{item.index}] would {action:<7} {item.describe()}{suffix}")
        print(
            f"\ndry-run：{len(items)} 条通过预检查，未写入任何文件"
            "（确认无误后加 --apply 执行）"
        )
        return EXIT_OK

    outcomes = [await migrate_one(ctx, item) for item in items]
    for outcome in outcomes:
        print(outcome.render())
    counts = {
        ACTION_MIGRATED: sum(1 for o in outcomes if o.action == ACTION_MIGRATED),
        ACTION_SKIPPED: sum(1 for o in outcomes if o.action == ACTION_SKIPPED),
        ACTION_FAILED: sum(1 for o in outcomes if o.action == ACTION_FAILED),
    }
    print(
        f"\nsummary: migrated={counts[ACTION_MIGRATED]} "
        f"skipped={counts[ACTION_SKIPPED]} failed={counts[ACTION_FAILED]}"
    )
    return EXIT_PARTIAL if counts[ACTION_FAILED] else EXIT_OK


def _items_from_args(args: argparse.Namespace) -> list[Item]:
    if args.file:
        if args.user or args.skill or args.to:
            raise MigrationError("--file 与 --user/--skill/--to 不能同时使用")
        return load_batch(Path(args.file))
    missing = [
        flag
        for flag, value in (("--user", args.user), ("--skill", args.skill), ("--to", args.to))
        if not value
    ]
    if missing:
        raise MigrationError(
            "单条模式需要 " + " ".join(missing) + "（批量迁移请用 --file migration.yaml）"
        )
    return [
        Item(
            0,
            args.user.strip(),
            args.skill.strip(),
            parse_target(args.to),
        )
    ]


async def _main_async(args: argparse.Namespace, items: list[Item]) -> int:
    db = await aiosqlite.connect(args.db)
    try:
        workspaces = WorkspaceStore(db)
        ctx = Context(
            db=db,
            checker=workspace_access_checker(workspaces),
            user_backend=LocalSkillBackend(args.user_skills_dir),
            project_root=args.project_skills_dir,
        )
        return await _run(items, ctx, apply=args.apply)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="edith-skill-migrate",
        description="把存量用户级 Skill 显式复制成项目级（默认 dry-run，源永不自动删）",
    )
    parser.add_argument("--user", help="用户 ID（单条模式）")
    parser.add_argument("--skill", help="Skill 目录名（单条模式）")
    parser.add_argument("--to", help="目标：project:<workspace_id> 或 user")
    parser.add_argument("--file", help="批量迁移 YAML（含 migrations 列表）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入；不加时只打印计划（默认 dry-run）",
    )
    parser.add_argument("--db", default=None, help="SQLite 路径（默认取 EDITH_DB_PATH）")
    parser.add_argument("--user-skills-dir", default=None, help="用户级权威副本根目录")
    parser.add_argument("--project-skills-dir", default=None, help="项目级权威副本根目录")
    args = parser.parse_args()

    if args.db is None:
        # 与 `trace/viewer.py` 同序：CLI 是独立进程，不 load 就永远读不到 .env 里的
        # `EDITH_DB_PATH`，只能落回相对路径默认值——从别的目录执行时会连到一个
        # 空库，然后报「用户不存在」，把「配错了」伪装成「没数据」。
        from ... import config

        config.load_environment()
        args.db = config.database_path()

    try:
        items = _items_from_args(args)
    except MigrationError as exc:
        print(f"拒绝执行：{exc}", file=sys.stderr)
        sys.exit(EXIT_REJECTED)

    try:
        code = asyncio.run(_main_async(args, items))
    except sqlite3.Error as exc:
        print(f"数据库不可用（{args.db}）：{exc}", file=sys.stderr)
        sys.exit(EXIT_REJECTED)
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover - 便于 `python -m`
    main()


__all__ = [
    "ACTION_FAILED",
    "ACTION_MIGRATED",
    "ACTION_SKIPPED",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXIT_REJECTED",
    "Context",
    "Item",
    "MigrationError",
    "Outcome",
    "Target",
    "load_batch",
    "main",
    "migrate_one",
    "parse_target",
    "precheck",
]
