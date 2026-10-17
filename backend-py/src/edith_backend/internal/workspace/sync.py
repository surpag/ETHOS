"""方案 B（B2/B3）：本地目录 ↔ 沙箱增量同步。

- B2 `sync_sandbox_to_local`：把沙箱里与本地不同（或本地缺失）的文件拉回本地目录，
  覆盖 `sandbox_write_file` 之外的盲区（如 `sandbox_run_command` 内 heredoc/脚本落盘）。
- B3 `sync_local_to_sandbox`：把本地新增/修改的文件推送到沙箱（工具调用前 / 用户发消息时）。

冲突处理（B4）不在本模块：本模块只做"单向覆盖"，冲突由上层状态表判定。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
from collections.abc import Iterable
from pathlib import Path

from e2b import FileType

from ...config import database_path
from .layout import WORKSPACE

logger = logging.getLogger(__name__)

_MAX_DEPTH = 20  # 沙箱目录递归深度上限

# 内置忽略：这些目录/文件是构建产物或版本库元数据，同步过去只会拖慢速度并污染沙箱视图
DEFAULT_IGNORES = [
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
    "*.pyc",
]

# ---------------------------------------------------------------------------
# 同步护栏（P0）
#
# 背景：B2（沙箱→本地回拉）在**每个写类工具调用后**运行，原实现对工作区内
# **每个**文件做 `files.read(format="bytes")` 全量字节比对。而 E2B SDK 的默认
# 请求超时是整请求硬超时 60s（e2b/connection_config.py: REQUEST_TIMEOUT=60.0），
# 于是工作区里的图片/大文件被反复拉回，且逐个各等 60s —— 20 个文件就是约
# 20 分钟阻塞在工具返回之前（用户观感："响应慢 / 像卡住"）。
#
# 三道护栏（互相独立，任一生效即可避免一次昂贵的远端读取）：
#   ① 免下载比对：本地已有同名文件且大小一致 → 不读字节（仅用 list 的 size）
#   ② 体积上限：单文件超过阈值 → 不参与自动回拉
#   ③ 二进制排除：图片/音视频/压缩包/本地二进制/字体 → 不参与自动回拉
# 外加熔断：单轮连续失败 N 次即中止本轮，把最坏耗时从 O(文件数×60s) 压到 O(N×超时)。
# ---------------------------------------------------------------------------
_DEFAULT_MAX_PULL_BYTES = 2 * 1024 * 1024  # 2 MiB
_DEFAULT_READ_TIMEOUT_S = 15.0
_DEFAULT_FAILURE_LIMIT = 3


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺省或非法时回退 default（不抛异常）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量；缺省或非法时回退 default（不抛异常）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


# 单文件回拉体积上限（超过则不自动回拉；文件仍在沙箱内，可通过文件面板直读）
SYNC_MAX_FILE_BYTES = _env_int("EDITH_SYNC_MAX_FILE_BYTES", _DEFAULT_MAX_PULL_BYTES)
# 单次 files.read 的显式超时（秒）——必须显式传入，否则用 SDK 默认 60s 整请求硬超时
SYNC_READ_TIMEOUT_S = _env_float("EDITH_SYNC_READ_TIMEOUT_S", _DEFAULT_READ_TIMEOUT_S)
# 单轮同步连续失败上限（达到即中止本轮，避免逐文件各等一个超时）
SYNC_FAILURE_LIMIT = _env_int("EDITH_SYNC_FAILURE_LIMIT", _DEFAULT_FAILURE_LIMIT)
# 是否把二进制文件也纳入回拉（默认否；置 EDITH_SYNC_PULL_BINARY_FILES=true 可恢复旧行为）
SYNC_PULL_BINARY_FILES = (os.getenv("EDITH_SYNC_PULL_BINARY_FILES") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ---------------------------------------------------------------------------
# 删除传播（P1）
#
# 背景：原有同步只做 upsert，从不删对侧，于是删除在语义上不可表达，形成死锁：
#   本地删 → 沙箱还有 → 任意写类工具后 B2 回拉（mkdir + write）→ 本地复活
#   沙箱删 → 本地还有 → 任意写类工具前 B3 推送 → 沙箱复活
# 更隐蔽的是 `sandbox_delete_path` 本身属于写类工具：它执行前会先 B3 推送，
# 等于「先把本地副本推回来，再删掉」——删干净了，但只要本地不动，下轮又回来。
#
# 设计：维护一份「两侧共识的文件集合」（synced，落盘持久化）。任何一次成功同步
# 后把该路径记入共识。每次同步只做一次集合差：
#   - B2：synced − 当前沙箱集合 = 沙箱侧已消失 → 从本地删除
#   - B3：synced − 当前本地集合 = 本地侧已消失 → 从沙箱删除
# **完全不关心「谁用什么方式删的」**：`sandbox_delete_path`、`run_command rm -rf`、
# `git checkout` 回滚、用户在 IDE 里手删，全部收敛为「共识集合里少了哪些路径」，
# 因此无需按删除来源写任何特例分支（也就不会漏掉下一种删除方式）。
#
# 误删是这里最贵的错误，因此护栏全部默认保守：
#   ① 无状态文件（首次同步 / 状态丢失）→ 只建基线，不传播
#   ② 沙箱重建（sandbox_id 变化）→ B2 不做删除传播（新沙箱天然为空，
#      否则会把本地整棵树删光）
#   ③ 单轮传播删除上限 SYNC_MAX_DELETES（默认 500），超限跳过并告警
#   ④ 本地文件集合为空而共识非空 → 视为本地目录异常（盘未挂载 / 被清空），跳过
#   ⑤ 开关 EDITH_SYNC_PROPAGATE_DELETES（默认开；置 false 退回纯 upsert 旧行为）
# ---------------------------------------------------------------------------
SYNC_PROPAGATE_DELETES = (os.getenv("EDITH_SYNC_PROPAGATE_DELETES") or "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SYNC_MAX_DELETES = _env_int("EDITH_SYNC_MAX_DELETES", 500)


def sync_state_dir() -> Path:
    """同步状态（共识集合）落盘目录：默认与数据库同级的 workspace-sync/。"""
    raw = (os.getenv("EDITH_SYNC_STATE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(database_path()).parent / "workspace-sync"


def _state_file(workspace_id: str, state_dir: Path | None) -> Path:
    return (state_dir or sync_state_dir()) / f"{workspace_id}.json"


def load_synced_paths(
    workspace_id: str, state_dir: Path | None = None
) -> tuple[set[str], str | None]:
    """读取「两侧共识文件集合」与它对应的 sandbox_id。

    返回 (paths, sandbox_id)。文件缺失 / 损坏 / 格式不符一律返回 (set(), None)，
    调用方据此走「只建基线、不传播」的安全路径。
    """
    path = _state_file(workspace_id, state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), None
    if not isinstance(raw, dict):
        return set(), None
    items = raw.get("paths")
    sandbox_id = raw.get("sandbox_id")
    if not isinstance(items, list):
        return set(), None
    return {str(p) for p in items if isinstance(p, str) and p}, (
        str(sandbox_id) if isinstance(sandbox_id, str) and sandbox_id else None
    )


def save_synced_paths(
    workspace_id: str,
    paths: Iterable[str],
    sandbox_id: str | None,
    state_dir: Path | None = None,
) -> None:
    """原子写入共识集合（tmp + replace），失败只告警不阻塞同步。"""
    target = _state_file(workspace_id, state_dir)
    payload = {"sandbox_id": sandbox_id, "paths": sorted(set(paths))}
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:  # 状态不可写不应影响文件同步本身
        logger.warning("save sync state %s failed: %s", target, exc)


def _is_suspicious_full_wipe(vanished: set[str], synced: set[str]) -> bool:
    """共识集合「整体」消失 → 更像环境异常而非用户删除。

    局部消失（用户删了某几个文件 / 某个子目录）是正常意图，正常传播；
    整体消失则可能是盘未挂载、目录被清空、沙箱被重置——此时自动传播会
    把另一侧也删空且不可逆，因此只告警、等下次人工确认。
    """
    return bool(vanished) and vanished >= synced


def _prune_empty_dirs(dirs: Iterable[Path], stop: Path) -> None:
    """自深向浅回收空目录，遇到 stop（本地工作区根）即止。

    `rmdir` 对非空目录会抛错，正好用作「非空就停」的判定，无需额外检查。
    """
    try:
        stop = stop.resolve()
    except OSError:
        return
    for directory in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        current = directory
        while True:
            try:
                if current.resolve() == stop:
                    break
            except OSError:
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def _remove_local_paths(local_root: Path, rels: Iterable[str]) -> list[str]:
    """把共识集合里「沙箱侧已消失」的路径从本地删除，返回实际删掉的路径。"""
    removed: list[str] = []
    parents: set[Path] = set()
    for rel in rels:
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        target = local_root / rel
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
                removed.append(rel)
                parents.add(target.parent)
        except OSError as exc:
            logger.warning("propagate delete local %s failed: %s", rel, exc)
    _prune_empty_dirs(parents, local_root)
    return removed


async def _remove_sandbox_paths(sandbox, mount: str, rels: Iterable[str]) -> list[str]:
    """把共识集合里「本地侧已消失」的路径从沙箱删除，返回实际删掉的路径。

    单文件失败（含路径本就不存在）只记 debug —— 删除是幂等收敛，不是事务。
    """
    removed: list[str] = []
    parents: set[str] = set()
    for rel in rels:
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        target = f"{mount}/{rel}"
        try:
            await sandbox.files.remove(target)
            removed.append(rel)
            parents.add(posixpath.dirname(target))
        except Exception as exc:  # noqa: BLE001 - 不存在/已被删掉，收敛即可
            logger.debug("propagate delete sandbox %s skipped: %s", target, exc)
    # 自深向浅回收空目录。
    # 注意：E2B `files.remove` 对目录是**递归删除**的，因此必须先 list 确认为空
    # 才能删——否则会连带删掉目录下未纳入本次传播的文件。
    for directory in sorted(parents, key=lambda p: p.count("/"), reverse=True):
        current = directory
        while current.startswith(mount + "/"):
            try:
                remaining = await sandbox.files.list(current, depth=1)
            except Exception:  # noqa: BLE001 - 目录不存在：停止上溯
                break
            if remaining:
                break
            try:
                await sandbox.files.remove(current)
            except Exception:  # noqa: BLE001 - 并发写入 / 已不存在
                break
            current = posixpath.dirname(current)
    return removed

# 二进制/媒体后缀：回拉价值低、代价高，默认不参与「沙箱→本地」回拉。
# 注意：**只影响 B2 回拉方向**——「本地→沙箱」推送不受影响（用户上传的图片
# 仍需进沙箱供 Agent 与工具使用），文件面板也仍可从沙箱直读这些文件。
# 未收录 office/PDF：它们常是 Agent 的交付产物，且受体积上限约束。
BINARY_SUFFIXES = (
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".psd", ".ai",
    # 音视频
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus",
    # 压缩包
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".rar", ".7z", ".jar",
    # 本地二进制 / 编译产物
    ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a", ".class", ".wasm",
    ".node", ".pyd",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
)


def is_binary_like(rel: str) -> bool:
    """按后缀判断是否为「回拉价值低、代价高」的二进制/媒体文件。"""
    dot = rel.rfind(".")
    if dot <= rel.rfind("/"):
        return False
    return rel[dot:].lower() in BINARY_SUFFIXES


def is_excluded_from_pull(
    rel: str, patterns: list[str], *, pull_binaries: bool | None = None
) -> bool:
    """B2 回拉是否跳过该文件：内置/自定义忽略规则 + 二进制护栏。

    与 `is_ignored` 分开，是为了让「本地→沙箱」推送保留二进制（用户上传的
    图片仍需进沙箱），只对回拉方向做减法。

    pull_binaries 缺省取模块级 SYNC_PULL_BINARY_FILES（可用环境变量回退旧行为）。
    """
    if is_ignored(rel, patterns):
        return True
    if pull_binaries is None:
        pull_binaries = SYNC_PULL_BINARY_FILES
    if pull_binaries:
        return False
    return is_binary_like(rel)


def _matches_local_size(target: Path, remote_size: int) -> bool:
    """免下载比对：本地已有普通文件且大小与远端一致 → 无需下载字节。

    只用 `files.list` 返回的 size（不发起任何读取）。本地缺失、非普通文件或
    大小不一致一律返回 False（保守：宁可多读一次，也不漏同步）。
    """
    try:
        if not target.is_file():
            return False
        return target.stat().st_size == remote_size
    except OSError:
        return False


def scan_local_fingerprint(local_root: Path) -> tuple[int, int, float]:
    """本地目录的廉价指纹（文件数、总字节、最新 mtime），用于"未变化则跳过同步"。

    只 stat 不读内容：远快于逐文件与沙箱比对。与沙箱身份一起做键，
    确保沙箱重建（sandbox_id 变化）时不会被误判为"无需同步"。
    """
    if not local_root.is_dir():
        return (0, 0, 0.0)
    ignore = _load_ignore(local_root)
    count = 0
    total = 0
    newest = 0.0
    for path in local_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if is_ignored(path.relative_to(local_root).as_posix(), ignore):
            continue
        count += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
    return (count, total, newest)


def _mount(workspace_id: str, prefix: str = "") -> str:
    """沙箱侧的同步根：workspace/{id}/ 或 workspace/{id}/{prefix}/。"""
    base = WORKSPACE.workspace_path(workspace_id)
    p = (prefix or "").strip().strip("/")
    return f"{base}/{p}" if p else base


def _iter_merge_candidates(src: Path, dst: Path, prefix: str):
    """产出「只新增不覆盖」合并会复制的文件：逐个 yield (相对路径, 源绝对路径)。

    规则（`plan_merge` 预览与实际合并共用，避免两处漂移）：
    - 跳过 `..` 穿越；
    - 跳过忽略项（内置 DEFAULT_IGNORES + dst 的 .edithignore，与日常同步对齐）；
    - 跳过 dst 中已存在的路径（绝不覆盖用户文件）。
    """
    if not src.is_dir():
        return
    p = (prefix or "").strip().strip("/")
    base = (src / p) if p else src
    if not base.is_dir():
        return
    ignore = _load_ignore(dst)
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if ".." in rel.split("/"):
            continue
        if is_ignored(rel, ignore):
            continue
        if (dst / rel).exists():
            continue
        yield rel, path


def plan_merge(src: Path, dst: Path, *, prefix: str = "") -> list[str]:
    """列出「只新增不覆盖」合并将会复制的相对路径（只读，不落盘）。

    供 UI 在真正合并前预览「还有 N 个文件未合并（在旧副本里）」。
    """
    return [rel for rel, _ in _iter_merge_candidates(src, dst, prefix)]


def merge_dir_add_only(src: Path, dst: Path, *, prefix: str = "") -> int:
    """把旧副本 src 中本地尚不存在的文件合并到 dst（绑定目录）。

    只新增、绝不覆盖用户已有文件（绑定已有工作区时的安全初始合并）。
    """
    copied = 0
    for rel, source in _iter_merge_candidates(src, dst, prefix):
        target = dst / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            copied += 1
        except OSError as exc:
            logger.warning("merge %s failed: %s", rel, exc)
    if copied:
        logger.info("merged %d files from legacy copy into %s", copied, dst)
    return copied


async def sync_sandbox_to_local(
    sandbox,
    workspace_id: str,
    local_root: Path,
    *,
    prefix: str = "",
    max_files: int = 5000,
    max_file_bytes: int | None = None,
    read_timeout: float | None = None,
    failure_limit: int | None = None,
    pull_binaries: bool | None = None,
    state_dir: Path | None = None,
) -> int:
    """把沙箱 workspace/{id}/[prefix/] 下与本地不一致的文件下载到 local_root，返回更新文件数。

    prefix：本地目录在沙箱文件树中的前缀（"选文件夹上传"创建的工作区会多一层目录名）。

    护栏（本函数是「每个写类工具调用后」都会跑的路径，必须便宜且可预期）：
    - **免下载比对**：本地已有且 size 一致 → 直接用 `files.list` 的元数据跳过，不读字节；
    - **体积上限** `max_file_bytes`：超过则不回拉（文件仍在沙箱，可用文件面板直读）；
    - **二进制排除** `pull_binaries=False`：图片/音视频/压缩包等不回拉；
    - **熔断** `failure_limit`：连续失败达阈值即中止本轮，把最坏耗时从
      「文件数 × 60s」压到「阈值 × 超时」；
    - **显式短超时** `read_timeout`：不传则沿用 SDK 默认 60s 整请求硬超时。

    各参数缺省时取模块级配置（可用 EDITH_SYNC_* 环境变量覆盖），显式传入仅用于测试。
    """
    max_file_bytes = SYNC_MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
    read_timeout = SYNC_READ_TIMEOUT_S if read_timeout is None else read_timeout
    failure_limit = SYNC_FAILURE_LIMIT if failure_limit is None else failure_limit
    pull_binaries = SYNC_PULL_BINARY_FILES if pull_binaries is None else pull_binaries

    mount = _mount(workspace_id, prefix)
    try:
        entries = await sandbox.files.list(mount, depth=_MAX_DEPTH)
    except Exception as exc:  # noqa: BLE001 - 目录不存在/沙箱不可用：跳过
        logger.warning("list sandbox %s failed: %s", mount, exc)
        return 0

    changed = 0
    skipped_same = skipped_large = skipped_binary = skipped_ignored = 0
    failures = 0
    aborted = False
    deleted = 0
    ignore = _load_ignore(local_root)

    # 沙箱现存文件全集（排除忽略项）——删除传播的判定依据，与「是否拉取」无关：
    # 二进制/超大文件虽然不回拉，但它们在沙箱侧消失时同样要传播到本地。
    remote_files: dict[str, int] = {}
    for entry in entries:
        if entry.type == FileType.DIR:
            continue
        rel = entry.path[len(mount):].lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        if is_ignored(rel, ignore):
            continue
        remote_files[rel] = int(entry.size or 0)

    # 删除传播：共识集合里「沙箱侧已消失」的路径 → 从本地删掉。
    # ② 沙箱重建守卫：新沙箱天然为空，此时做差集会删光本地，故要求 sandbox_id 一致。
    synced, synced_sandbox_id = load_synced_paths(workspace_id, state_dir)
    sandbox_id = getattr(sandbox, "sandbox_id", None)
    same_sandbox = sandbox_id is not None and sandbox_id == synced_sandbox_id
    if SYNC_PROPAGATE_DELETES and synced and same_sandbox:
        vanished = synced - remote_files.keys()
        if vanished:
            if _is_suspicious_full_wipe(vanished, synced):
                # ④ 整体消失：疑似沙箱被重置 / list 拿到空树，不自动删本地
                logger.warning(
                    "B2 skip delete propagation ws=%s: all %d consensus paths vanished "
                    "(suspicious full wipe)",
                    workspace_id,
                    len(synced),
                )
            # ③ 单轮上限：超过视为状态异常（而非用户真的删了这么多），跳过并告警
            elif len(vanished) > SYNC_MAX_DELETES:
                logger.warning(
                    "B2 skip delete propagation ws=%s: %d vanished paths > limit %d",
                    workspace_id,
                    len(vanished),
                    SYNC_MAX_DELETES,
                )
            else:
                removed = _remove_local_paths(local_root, vanished)
                synced -= vanished
                deleted = len(removed)
                if deleted:
                    logger.info(
                        "B2 propagated %d deletions sandbox->local ws=%s",
                        deleted,
                        workspace_id,
                    )

    for entry in entries:
        if entry.type == FileType.DIR:
            continue
        rel = entry.path[len(mount):].lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        if is_excluded_from_pull(rel, ignore, pull_binaries=pull_binaries):
            if is_ignored(rel, ignore):
                skipped_ignored += 1
            else:
                skipped_binary += 1
            continue
        remote_size = int(entry.size or 0)
        if remote_size > max_file_bytes:
            skipped_large += 1
            continue
        target = local_root / rel
        # ① 免下载比对：本地已有且大小一致 → 不发起任何远端读取
        if _matches_local_size(target, remote_size):
            skipped_same += 1
            synced.add(rel)  # 两侧一致即共识，纳入删除传播基线
            continue
        try:
            # ⑤ 显式短超时：绝不用 SDK 默认的 60s 整请求硬超时
            raw = await sandbox.files.read(
                entry.path, format="bytes", request_timeout=read_timeout
            )
            data = bytes(raw)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断
            failures += 1
            logger.warning(
                "read sandbox file %s failed (%d/%d): %s",
                entry.path,
                failures,
                failure_limit,
                exc,
            )
            # ④ 熔断：连续失败达阈值即中止本轮，避免逐文件各等一个超时
            if failures >= failure_limit:
                aborted = True
                logger.warning(
                    "B2 sync aborted after %d consecutive failures ws=%s: "
                    "剩余文件本轮不再尝试（下轮重试）",
                    failures,
                    workspace_id,
                )
                break
            continue
        failures = 0
        try:
            if target.is_file() and target.read_bytes() == data:
                synced.add(rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            changed += 1
            synced.add(rel)
        except OSError as exc:
            logger.warning("write local file %s failed: %s", target, exc)
            continue
        if changed >= max_files:
            logger.warning("sync aborted at max_files=%d", max_files)
            break
    if SYNC_PROPAGATE_DELETES:
        save_synced_paths(workspace_id, synced, sandbox_id, state_dir)
    if changed or deleted or skipped_same or skipped_large or skipped_binary or failures:
        logger.info(
            "B2 sync ws=%s changed=%d deleted=%d skipped(same=%d large=%d binary=%d "
            "ignored=%d) failures=%d%s",
            workspace_id,
            changed,
            deleted,
            skipped_same,
            skipped_large,
            skipped_binary,
            skipped_ignored,
            failures,
            " aborted" if aborted else "",
        )
    else:
        logger.debug("B2 sync ws=%s no-op", workspace_id)
    return changed


async def sync_local_to_sandbox(
    sandbox,
    workspace_id: str,
    local_root: Path,
    *,
    prefix: str = "",
    max_files: int = 5000,
    max_file_bytes: int = 10 * 1024 * 1024,
    failure_limit: int | None = None,
    state_dir: Path | None = None,
) -> int:
    """把本地目录中新增/修改的文件推送到沙箱 workspace/{id}/[prefix/]，返回上传文件数。

    以"本地与沙箱大小不一致即上传"为判定（B3 语义：本地为准），
    忽略 `.edithignore` 与超过 max_file_bytes 的大文件。

    免下载比对：远端大小由**一次 `files.list`** 得到，不再逐文件
    `files.read` 回来比内容（旧实现的 N 次远端读取是第二个慢点）。

    删除传播：共识集合里「本地侧已消失」的路径 → 从沙箱删掉（本地为准）。
    """
    if not local_root.is_dir():
        return 0
    failure_limit = SYNC_FAILURE_LIMIT if failure_limit is None else failure_limit
    mount = _mount(workspace_id, prefix)
    ignore = _load_ignore(local_root)

    # 本地现存文件全集（排除忽略项）：既是推送源，也是删除传播的判定依据
    local_files: dict[str, Path] = {}
    for path in local_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        if is_ignored(rel, ignore):
            continue
        local_files[rel] = path

    # 远端尺寸表：一次 list 拿全，替代逐文件远端读取
    remote_sizes: dict[str, int] | None = {}
    try:
        entries = await sandbox.files.list(mount, depth=_MAX_DEPTH)
        remote_sizes = {
            e.path: int(e.size or 0) for e in entries if e.type != FileType.DIR
        }
    except Exception as exc:  # noqa: BLE001 - list 失败则退化为"不比对、直接推"
        logger.warning("list sandbox %s failed, push without compare: %s", mount, exc)
        remote_sizes = None

    # 删除传播：共识集合里「本地侧已消失」的路径 → 从沙箱删掉（本地为准）。
    # 本地目录本身不存在时已在函数入口返回；目录存在但为空是合法的「用户清空」，
    # 但「整体消失」仍按可疑处理（见 _is_suspicious_full_wipe）。
    # 远端尺寸表拿不到（list 失败）时不传播，避免在看不见远端时盲删。
    synced, _ = load_synced_paths(workspace_id, state_dir)
    deleted = 0
    if SYNC_PROPAGATE_DELETES and synced and remote_sizes is not None:
        vanished = synced - local_files.keys()
        if vanished:
            if _is_suspicious_full_wipe(vanished, synced):
                # ④ 整体消失：疑似本地盘未挂载 / 目录被清空，不自动删沙箱
                logger.warning(
                    "B3 skip delete propagation ws=%s: all %d consensus paths vanished "
                    "(suspicious full wipe)",
                    workspace_id,
                    len(synced),
                )
            elif len(vanished) > SYNC_MAX_DELETES:
                logger.warning(
                    "B3 skip delete propagation ws=%s: %d vanished paths > limit %d",
                    workspace_id,
                    len(vanished),
                    SYNC_MAX_DELETES,
                )
            else:
                removed = await _remove_sandbox_paths(sandbox, mount, vanished)
                for rel in removed:
                    remote_sizes.pop(f"{mount}/{rel}", None)
                synced -= vanished
                deleted = len(removed)
                if deleted:
                    logger.info(
                        "B3 propagated %d deletions local->sandbox ws=%s",
                        deleted,
                        workspace_id,
                    )

    changed = 0
    skipped_same = skipped_large = 0
    failures = 0
    aborted = False
    for rel, path in local_files.items():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            skipped_large += 1
            continue
        remote = f"{mount}/{rel}"
        # 免下载比对：远端已存在且大小一致 → 视为一致，不上传
        if remote_sizes is not None and remote_sizes.get(remote) == size:
            skipped_same += 1
            synced.add(rel)  # 两侧一致即共识
            continue
        try:
            await sandbox.files.write(remote, path.read_bytes())
            changed += 1
            synced.add(rel)
            if remote_sizes is not None:
                remote_sizes[remote] = size
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断
            failures += 1
            logger.warning(
                "sync local file %s failed (%d/%d): %s",
                rel,
                failures,
                failure_limit,
                exc,
            )
            if failures >= failure_limit:
                aborted = True
                logger.warning(
                    "B3 sync aborted after %d consecutive failures ws=%s",
                    failures,
                    workspace_id,
                )
                break
            continue
        failures = 0
        if changed >= max_files:
            break
    if SYNC_PROPAGATE_DELETES:
        save_synced_paths(workspace_id, synced, getattr(sandbox, "sandbox_id", None), state_dir)
    if changed or deleted or skipped_large or failures:
        logger.info(
            "B3 sync ws=%s changed=%d deleted=%d skipped(same=%d large=%d) failures=%d%s",
            workspace_id,
            changed,
            deleted,
            skipped_same,
            skipped_large,
            failures,
            " aborted" if aborted else "",
        )
    else:
        logger.debug("B3 sync ws=%s no-op (unchanged=%d)", workspace_id, skipped_same)
    return changed


def _load_ignore(local_root: Path) -> list[str]:
    """读取 .edithignore（每行一个简单模式；支持目录名与后缀）。"""
    f = local_root / ".edithignore"
    if not f.is_file():
        return []
    try:
        return [
            line.strip()
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return []


def is_ignored(rel: str, patterns: list[str]) -> bool:
    """是否忽略该相对路径（内置忽略 + .edithignore）。"""
    parts = rel.split("/")
    for pat in DEFAULT_IGNORES + patterns:
        p = pat.rstrip("/")
        if not p:
            continue
        if p.startswith("*"):
            if rel.endswith(p[1:]):
                return True
        elif p in parts or rel == p or rel.startswith(p + "/"):
            return True
    return False
