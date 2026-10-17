"""工作区 Volume 后端抽象：E2B 与本地文件系统两种实现。

对应 `docs/p1-workspace-volume-design.md` 的 Volume 依赖收敛：
- E2BVolumeBackend：真实 E2B Volume（懒创建 + 挂载到沙箱）
- LocalVolumeBackend：本地目录模拟 Volume（不消耗 E2B 配额，
  沙箱创建后由 SandboxService 把本地文件同步进沙箱目录）
按环境变量 EDITH_WORKSPACE_VOLUME_BACKEND=e2b|local 选择，默认 e2b。
"""

from __future__ import annotations

import logging
import os
import posixpath
from abc import ABC, abstractmethod
from pathlib import Path

from e2b import AsyncSandbox, AsyncVolume

from .layout import WORKSPACE
from .store import WorkspaceStore
from .sync import is_ignored

logger = logging.getLogger(__name__)

# 本地模拟 Volume 的根目录（环境变量可覆盖）
_LOCAL_ROOT = Path(os.getenv("EDITH_WORKSPACE_VOLUME_DIR", "workspace-volumes"))

# 全量推送分块上限：单次 write_files 请求过大（文件数/字节数）会让 E2B 调用超时
SYNC_BATCH_FILES = 100
SYNC_BATCH_BYTES = 4 * 1024 * 1024
# 单文件上限：超过则跳过（大二进制同步价值低且会拖垮请求）
SYNC_MAX_FILE_BYTES = 10 * 1024 * 1024


class WorkspaceSyncError(Exception):
    """本地工作区文件同步到沙箱失败（暂时性，可重试）。"""


class VolumeBackend(ABC):
    """工作区文件存储的抽象边界。"""

    @abstractmethod
    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        """写入一批文件（Volume 内相对路径），返回成功写入数。"""

    @abstractmethod
    async def destroy(self, user_id: str, workspace_id: str) -> None:
        """销毁工作区对应的持久化存储。"""

    async def ensure_volume(self, user_id: str, workspace_id: str) -> str | None:
        """确保工作区有持久化 Volume 并返回其名称；无挂载语义（Local 后端）返回 None。

        E2B 后端懒创建 Volume（首次访问时）；Local 后端靠沙箱同步，无独立卷，返回 None。
        """
        return None

    async def sync_workspace_to_sandbox(self, workspace_id: str, sandbox: AsyncSandbox) -> None:
        """把指定工作区文件同步进沙箱。

        E2B 后端靠挂载自动可见（no-op）；Local 后端把本地目录写入沙箱。
        """


class E2BVolumeBackend(VolumeBackend):
    """真实 E2B Volume：懒创建 + AsyncVolume 写入/销毁；沙箱靠挂载无需同步。"""

    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        volume = await self._ensure(user_id, workspace_id)
        written = 0
        for rel_path, content in files:
            try:
                dir_path = posixpath.dirname(rel_path)
                if dir_path:
                    await volume.make_dir(dir_path, force=True)
                await volume.write_file(rel_path, content, force=True)
                written += 1
            except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断
                logger.error("failed to write %s: %s", rel_path, exc)
        return written

    async def _ensure(self, user_id: str, workspace_id: str) -> AsyncVolume:
        record = await self._store.load_volume(user_id, workspace_id)
        if record is not None:
            volume_id, _name, _token = record
            try:
                return await AsyncVolume.connect(volume_id)
            except Exception as exc:
                # 重建会造出一个新的空卷 + 覆盖 DB 绑定 → 旧卷成孤儿、用户数据"消失"。
                # 网络抖动等暂时性故障应显式失败（上层告警/降级），而不是静默丢数据。
                logger.error("reconnect volume %s failed: %s", volume_id, exc)
                raise
        volume = await AsyncVolume.create(name=f"ws_{workspace_id[:12]}_{user_id[:8]}")
        await self._store.save_volume(
            user_id, workspace_id, volume.volume_id, volume.name, volume.token
        )
        logger.info("created volume %s for workspace %s", volume.volume_id, workspace_id)
        return volume

    async def ensure_volume(self, user_id: str, workspace_id: str) -> str | None:
        """确保工作区有 E2B Volume（懒创建），返回 volume_name 供沙箱挂载。"""
        volume = await self._ensure(user_id, workspace_id)
        return volume.name

    async def destroy(self, user_id: str, workspace_id: str) -> None:
        record = await self._store.load_volume(user_id, workspace_id)
        if record is None:
            return
        try:
            await AsyncVolume.destroy(record[0])
            logger.info("destroyed volume %s", record[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to destroy volume %s: %s", record[0], exc)


class LocalVolumeBackend(VolumeBackend):
    """本地目录模拟 Volume：写本地磁盘；沙箱创建后由 SandboxService 同步。"""

    def __init__(self, root: Path | str = _LOCAL_ROOT) -> None:
        self._root = Path(root) if not isinstance(root, Path) else root
        # 方案 B（B1a）：workspace_id → 用户本地目录绝对路径（绑定后直接作为存储根）
        self._local_roots: dict[str, Path] = {}
        # 方案 B：workspace_id → 本地目录在沙箱文件树中的前缀
        # （"选文件夹上传"创建的工作区，卷里会多一层目录名，如 Data/；空 = 直接对应根）
        self._prefixes: dict[str, str] = {}

    def bind_local_root(
        self, workspace_id: str, local_root: str, prefix: str = ""
    ) -> None:
        """把 workspace 绑定到用户本地目录（消除 volume 副本）。"""
        self._local_roots[workspace_id] = Path(local_root)
        self._prefixes[workspace_id] = (prefix or "").strip().strip("/")

    def unbind_local_root(self, workspace_id: str) -> None:
        """解除本地目录绑定（不动用户文件）。"""
        self._local_roots.pop(workspace_id, None)
        self._prefixes.pop(workspace_id, None)

    def local_root_of(self, workspace_id: str) -> Path | None:
        """返回绑定的本地目录（未绑定返回 None）。"""
        return self._local_roots.get(workspace_id)

    def local_root_prefix_of(self, workspace_id: str) -> str:
        """返回绑定的前缀（未绑定或空前缀返回 ""）。"""
        return self._prefixes.get(workspace_id, "")

    def legacy_dir(self, workspace_id: str) -> Path:
        """绑定前的副本目录（workspace-volumes/{id}）；用于绑定时的存量文件合并。"""
        return self._root / workspace_id

    def _strip_prefix(self, rel: str, workspace_id: str) -> str | None:
        """把 workspace 内相对路径转为本地相对路径；不在绑定子树下返回 None。"""
        prefix = self._prefixes.get(workspace_id, "")
        rel = rel.strip("/")
        if not prefix:
            return rel
        if not rel:
            # 有前缀时，workspace 根目录不对应本地目录（应看沙箱，保持与 Agent 视图一致）
            return None
        if rel == prefix:
            return ""
        if rel.startswith(prefix + "/"):
            return rel[len(prefix) + 1 :]
        return None

    def _dir(self, workspace_id: str) -> Path:
        # B1b 读写直连：绑定 local_root 时所有存储操作都落在用户目录，否则退回副本
        return self._local_roots.get(workspace_id) or (self._root / workspace_id)

    def _read_dir(self, workspace_id: str) -> Path:
        return self._dir(workspace_id)

    # 本次新增（P0 性能优化）：把 workspace/{workspace_id}/... 相对路径映射到本地磁盘路径
    def resolve_workspace_path(self, relative: str) -> Path | None:
        """仅当 relative 匹配 workspace/{id}/ 且对应本地目录存在时返回路径；否则 None。

        调用方（Sandbox HTTP）命中后直接本地直读，绕开 E2B 网络往返。
        路径安全：拒绝 .. 穿越与绝对路径子段。
        """
        rel = (relative or "").strip().replace("\\", "/")
        if not rel or rel == ".":
            return None
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] != "workspace" or not parts[1]:
            return None
        workspace_id = parts[1]
        target = self._read_dir(workspace_id)
        if not target.exists():
            return None
        sub = "/".join(parts[2:])
        sub = self._strip_prefix(sub, workspace_id)
        if sub is None:
            # 该路径不在绑定的本地子树内（有前缀时，前缀之外仍走沙箱）
            return None
        if sub:
            if sub.startswith("/") or ".." in sub.split("/"):
                logger.warning("skip unsafe local sub path: %s", sub)
                return None
            target = target / sub
        return target

    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        base = self._dir(workspace_id)
        base.mkdir(parents=True, exist_ok=True)
        written = 0
        for rel_path, content in files:
            # 路径安全：拒绝绝对路径与 .. 穿越（http 层已校验，此处再兜底）
            if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
                logger.warning("skip unsafe local path: %s", rel_path)
                continue
            # 前缀映射：卷内路径（如 Data/backend/x.py）→ 本地相对路径
            local_rel = self._strip_prefix(rel_path, workspace_id)
            if local_rel is None:
                logger.warning("skip path outside bound local subtree: %s", rel_path)
                continue
            if not local_rel:
                continue
            target = base / local_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            written += 1
        logger.info("local volume wrote %d files for %s", written, workspace_id)
        return written

    async def destroy(self, user_id: str, workspace_id: str) -> None:
        # 安全：绑定的是用户本地目录时，只解绑、绝不删除用户文件
        if workspace_id in self._local_roots:
            self.unbind_local_root(workspace_id)
            logger.info("unbound local root for %s (user dir kept)", workspace_id)
            return
        import shutil

        target = self._dir(workspace_id)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            logger.info("local volume removed %s", workspace_id)

    async def sync_workspace_to_sandbox(
        self, workspace_id: str, sandbox: AsyncSandbox
    ) -> None:
        """把工作区文件全量推送进沙箱。

        两个曾经导致线上问题的点：
        1. **必须应用前缀**——带前缀的工作区若推到 workspace/{id}/ 根，会与原有
           {prefix}/ 子树形成两份副本（用户看到"两个项目"）；
        2. **必须过滤 + 分块**——一次性 write_files 全部文件（含 .venv/node_modules
           这类上万个文件）会让单次请求超时（用户看到"同步失败: timed out"）。
        """
        base = self._dir(workspace_id)
        if not base.exists():
            return
        mount = WORKSPACE.workspace_path(workspace_id)
        prefix = self._prefixes.get(workspace_id, "")
        if prefix:
            mount = f"{mount}/{prefix}"

        batches: list[list[dict]] = []
        current: list[dict] = []
        current_bytes = 0
        skipped_ignored = 0
        skipped_large = 0
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if is_ignored(rel, []):
                skipped_ignored += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > SYNC_MAX_FILE_BYTES:
                skipped_large += 1
                continue
            current.append({"path": f"{mount}/{rel}", "data": path.read_bytes()})
            current_bytes += size
            if len(current) >= SYNC_BATCH_FILES or current_bytes >= SYNC_BATCH_BYTES:
                batches.append(current)
                current, current_bytes = [], 0
        if current:
            batches.append(current)
        if not batches:
            logger.info(
                "local volume nothing to sync for %s (ignored=%d large=%d)",
                workspace_id,
                skipped_ignored,
                skipped_large,
            )
            return
        try:
            for batch in batches:
                await sandbox.files.write_files(batch)
        except Exception as exc:
            raise WorkspaceSyncError(f"workspace {workspace_id} 同步失败: {exc}") from exc
        logger.info(
            "local volume synced %d files in %d batches to sandbox for %s (ignored=%d large=%d)",
            sum(len(b) for b in batches),
            len(batches),
            workspace_id,
            skipped_ignored,
            skipped_large,
        )


def new_backend(kind: str, store: WorkspaceStore) -> VolumeBackend:
    """按环境变量构造后端；未知值回退 E2B 并告警。"""
    if kind == "local":
        logger.info("workspace volume backend: local")
        return LocalVolumeBackend()
    logger.info("workspace volume backend: e2b")
    return E2BVolumeBackend(store)
