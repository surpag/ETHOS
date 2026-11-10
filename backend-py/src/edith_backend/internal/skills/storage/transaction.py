"""权威副本的目录级替换协议（设计稿 7.4 / D9）。

为什么不是逐文件覆盖
------------------
`workspace/sync.py` 的 pull 是逐文件 `write_bytes`。对 Skill 来说这有两个不可接受
的失败模式：

1. 模型把 `SKILL.md` 写坏（少一个 `---`），pull 之后**权威副本就坏了**，下一轮
   Discovery 该 Skill 直接从索引消失 —— 用户改一次就丢掉了原来能用的版本；
2. 半途失败留下「一半新一半旧」的目录，没有任何一个时刻是一致的。

所以 pull 先落到 `.staging-<ts>/`，校验通过后整体替换。

粒度为什么是「一个 Skill 一个目录」而不是整棵树一次 rename
--------------------------------------------------------
`.conflicts/`、`.rejected/`、`overview.md`、`.edithignore` 是服务端文件，必须留在
原地不动；整树 rename 会把它们一起搬走，而它们恰恰是「上一版为什么被拒」的证据。

Windows 为什么必须三步
--------------------
POSIX 的 `rename(2)` 对已存在的目标目录可以原子替换；Windows 上 `os.replace`
对目录会失败（目标已存在）。可行协议只有「旧 → `.old-<ts>`，暂存 → 正式，
删 `.old`」。**任一步崩溃都必须能恢复**，因此 `recover()` 在每次访问前运行：
发现「正式目录缺失、`.old` 里还在」就搬回去；发现「两边都在」说明新版本已就位，
`.old` 里那份是本次替换的旧版残留，删掉即可。这不是健壮性加分，而是
「崩溃窗口会不会吃掉全部 Skill」的差别。
"""

from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 候选内容的暂存前缀（每次 pull 一个，同一目录内可并存多个进程的暂存）
STAGING_PREFIX = ".staging-"
#: 替换前旧版本的临时名（崩溃恢复靠它）
OLD_PREFIX = ".old-"
#: 校验未通过、从未生效的候选版本
REJECTED_DIR = ".rejected"

#: 暂存目录超过这个年龄即视为「崩溃残留」而非「正在进行的 pull」。
#: pull 的每一步都有超时与熔断，一次 pull 不可能跑两小时；反过来，
#: 阈值太小会把正在写入的暂存目录删掉，让本次 pull 直接失败。
STALE_STAGING_SECONDS = 2 * 60 * 60

#: 每次替换的临时目录名都用「时间戳 + 随机后缀」：同一秒内的两次 pull
#: （多副本 / 重试）撞名会让恢复逻辑把别人的暂存当成自己的旧版。
def _stamp() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def _remove_tree(path: Path) -> bool:
    """尽力删除一棵树，返回是否已彻底删除；**任何失败都不上抛**。

    为什么不能用 `shutil.rmtree(ignore_errors=True)`：它只吞 `OSError`。
    `SystemExit`（环境注入的删除护栏）、以及 Windows 上偶发的
    非 `OSError` 异常都会穿出去——而调用点全都在「数据已经提交完」之后，
    异常一穿，本来成功的 pull 被报成失败，甚至把整个进程带下去
    （`SystemExit` 从 asyncio 任务里冒出来会终止事件循环）。

    所以：先整树删；失败就退化成自底向上逐个删，单文件失败只跳过它。
    `CancelledError` / `KeyboardInterrupt` 不在捕获范围内，取消照常传播。
    """
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except (OSError, SystemExit) as exc:
        logger.warning("remove tree failed, falling back to per-entry: %s: %s", path, exc)
    removed_all = True
    try:
        entries = sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    except (OSError, SystemExit):
        return False
    for entry in entries:
        try:
            entry.unlink() if entry.is_file() or entry.is_symlink() else entry.rmdir()
        except (OSError, SystemExit):
            removed_all = False
    try:
        path.rmdir()
    except FileNotFoundError:
        pass
    except (OSError, SystemExit):
        removed_all = False
    if not removed_all:
        logger.warning("remove tree incomplete, left in place for retry: %s", path)
    return removed_all


def purge_stale_staging(directory: Path, max_age_seconds: int = STALE_STAGING_SECONDS) -> int:
    """清掉过期的 `.staging-*`，返回清理个数。

    取消与崩溃都会留下暂存目录（建了目录、还没走到 commit）。它们不参与索引、
    也不参与同步，但会一直躺着；每崩一次多一个。按年龄判定而不是「看到就删」，
    是因为并发 pull（多副本 / 同一用户两个会话）的暂存也长这样。
    """
    purged = 0
    try:
        leftovers = list(directory.glob(f"{STAGING_PREFIX}*"))
    except OSError:
        return purged
    for staging in leftovers:
        if not staging.is_dir():
            continue
        try:
            age = time.time() - staging.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        if _remove_tree(staging):
            purged += 1
            logger.warning("purged stale skill staging age=%.0fs dir=%s", age, staging.name)
    return purged


def new_staging(directory: Path) -> Path:
    """在权威目录内建一个暂存目录（必须在同一文件系统上，否则 rename 跨盘失败）。"""
    staging = directory / f"{STAGING_PREFIX}{_stamp()}"
    staging.mkdir(parents=True, exist_ok=False)
    return staging


def discard(staging: Path) -> None:
    """丢弃暂存目录（权威副本零变化）。

    用 `_remove_tree` 而不是 `shutil.rmtree(ignore_errors=True)`：
    失败路径上清理不掉只该留下垃圾，不该让「本来要返回 unavailable 的错误」
    变成另一个异常。
    """
    _remove_tree(staging)


def reject(staging: Path, directory: Path) -> str:
    """把校验未通过的候选版本移进 `.rejected/<ts>/`，返回相对路径。

    与 `.conflicts/<ts>/` 语义不同，不能混用：那是「覆盖本地之前留存的旧版本」，
    这是「从未生效的候选版本」。两者都要能被人找到，都不参与同步。
    """
    target_dir = directory / REJECTED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / _stamp()
    try:
        staging.rename(destination)
    except OSError as exc:
        logger.warning("move rejected skill staging failed: %s", exc)
        discard(staging)
        return ""
    return f"{REJECTED_DIR}/{destination.name}"


def recover(directory: Path) -> list[str]:
    """回滚崩溃残留的 `.old-*`，返回被恢复的顶层目录名。

    三个分支必须都在：缺正式目录 → 搬回；两边都在 → 新版本已就位，删旧残留；
    `.old` 已空 → 删掉空目录。只处理 `.` 开头的临时目录，绝不触碰真实 Skill。

    `failed` 是**每个 `.old` 目录各自**的状态，不是全局的：一个残留目录里有
    文件搬不回去，不该让同批其它残留目录也得不到清理（否则一次失败会让
    旧版本残留永久堆积，而它们每次访问都要重新遍历一遍）。
    """
    restored: list[str] = []
    try:
        leftovers = sorted(directory.glob(f"{OLD_PREFIX}*"))
    except OSError:
        return restored
    for old in leftovers:
        if not old.is_dir():
            continue
        blocked = False
        try:
            children = sorted(old.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("recover cannot list %s: %s", old.name, exc)
            continue
        for child in children:
            target = directory / child.name
            if target.exists():
                # 新版本已在位：这份是替换前的旧版，留着只会让下次恢复判不清
                continue
            try:
                child.rename(target)
                restored.append(child.name)
                logger.warning(
                    "recovered skill from interrupted replace dir=%s name=%s",
                    directory.name,
                    child.name,
                )
            except OSError as exc:
                # 搬不回去就**不能删** `.old`：那是这份内容唯一的副本
                blocked = True
                logger.warning("recover skill %s failed: %s", child.name, exc)
        if not blocked:
            _remove_tree(old)
    return restored


def commit(staging: Path, directory: Path, *, allow_delete: bool = True) -> int:
    """把暂存目录的内容整体替换进权威目录，返回替换的顶层条目数。

    两个方向都要做，缺一个就不是「替换」而是「合并」：
    - 暂存有的 → 搬进权威目录（覆盖同名旧目录）；
    - 权威有而暂存没有的 → 搬进 `.old` 后随 `.old` 一起删除（删除传播）。

    `allow_delete=False` 时只做第一个方向：同名覆盖照旧，但**不删**候选里没有的旧目录。
    语义是「这次候选不配当删除的证人」，不是「合并」的退让：
    删除是唯一不可逆的操作，只有在能证明「这份候选见过当前权威内容」时才允许
    （调用方 `LocalSkillBackend.pull` 用「本地与这个沙箱上次同步后没变过」来证明）。
    没有这条，任何一个内容过期的沙箱回拉一次，就会把它没见过的 Skill 从权威副本删掉。

    只搬非 `.` 开头的条目：`.edithignore`、`.conflicts/`、`.rejected/`、
    `overview.md` 是服务端文件，由 `ensure()` / `read_index()` 维护，
    让它们参与替换只会让「谁写了它」变得含糊。

    失败时的处置是「保留 `.old` 并抛出」，绝不清理：中途失败时部分旧版本已经被
    搬到 `.old`，此时删掉它就是把这部分 Skill 彻底删掉。清理只发生在成功路径上，
    失败路径交给 `recover()`（缺的搬回、两边都在则删旧残留）。

    成功路径上的清理同样是**尽力而为**：`.old` 删不掉（Windows 占用、环境护栏）
    只留一份垃圾，下次 `recover()` 会再试；把它算成 commit 失败会谎报数据没落地
    ——那时权威目录里其实已经是新版本了。
    """
    stamp = _stamp()
    old = directory / f"{OLD_PREFIX}{stamp}"
    moved = 0
    try:
        incoming = sorted(
            (child for child in staging.iterdir() if not child.name.startswith(".")),
            key=lambda p: p.name,
        )
        keep = {child.name for child in incoming}
        for child in incoming:
            target = directory / child.name
            if target.exists():
                old.mkdir(parents=True, exist_ok=True)
                target.rename(old / child.name)
            child.rename(target)
            moved += 1
        # 候选里没有的旧目录：只有证明得了「候选见过当前权威内容」时才删
        extras = sorted(directory.iterdir(), key=lambda p: p.name) if allow_delete else []
        for child in extras:
            # 只删除「目录」：Skill 是目录，而根目录下的文件（overview.md /
            # .edithignore / 误落的杂项）不是 Skill，删掉它们不属于删除传播
            if child.name.startswith(".") or child.name in keep or not child.is_dir():
                continue
            old.mkdir(parents=True, exist_ok=True)
            child.rename(old / child.name)
    except OSError as exc:
        logger.warning(
            "commit skill staging failed dir=%s moved=%d error=%s (kept %s for recovery)",
            directory.name,
            moved,
            exc,
            old.name,
        )
        raise
    _remove_tree(old)
    discard(staging)
    return moved


def pending_recovery(directory: Path) -> bool:
    """是否存在崩溃残留（供调用方决定是否要跑 `recover()`）。"""
    try:
        return any(directory.glob(f"{OLD_PREFIX}*"))
    except OSError:
        return False
