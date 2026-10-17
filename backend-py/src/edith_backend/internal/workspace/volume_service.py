"""工作区 Volume 服务：门面，按后端实现（E2B / 本地）代理写入与销毁。

对应 `docs/p1-workspace-volume-design.md`：
- 懒创建：首次上传文件才创建持久化存储（E2B Volume 或本地目录）
- 写入：委托 VolumeBackend（Volume 内相对路径）
- 销毁：删除工作区时调用（通过 WorkspaceStore.delete 的 on_destroy_volume 回调注入）
E2B 依赖收敛在 volume_backend.py，便于单测 mock 与本地开发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .store import WorkspaceStore
from .volume_backend import LocalVolumeBackend, VolumeBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BindOutcome:
    """绑定本地目录的结果：本次合并文件数 + 旧副本中仍未合并（本地缺失）的文件数。"""

    merged_files: int = 0
    unmerged_files: int = 0


class WorkspaceVolumeService:
    """工作区文件存储的门面：按后端实现代理写入、同步与销毁。"""

    def __init__(self, store: WorkspaceStore, backend: VolumeBackend) -> None:
        self._store = store
        self._backend = backend
        # 标记需要重新同步到沙箱的 workspace_id（文件写入后置 dirty，sync 成功后清除）
        self._dirty: set[str] = set()
        # 已从 DB 加载过 local_root 的 workspace_id（避免每次读都查库）
        self._local_root_loaded: set[str] = set()

    # 本次新增：暴露后端实例，供 Sandbox 层判断是否可本地直读（P0 性能优化）
    @property
    def backend(self) -> VolumeBackend:
        """当前工作区存储后端（E2BVolumeBackend / LocalVolumeBackend）。"""
        return self._backend

    async def write_files(
        self, user_id: str, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> int:
        """写入一批文件到工作区存储（路径为 Volume 内相对路径）。"""
        if not files:
            return 0
        written = await self._backend.write_files(user_id, workspace_id, files)
        written_paths = [p for p, _ in files[:written]]
        if written_paths:
            await self._store.update_files_metadata(
                user_id, workspace_id, written_paths
            )
            # 文件已写入本地：标记该 workspace 需要重新同步到沙箱（dirty）
            self._dirty.add(workspace_id)
        logger.info(
            "wrote %d files to workspace %s (%s total)",
            written,
            workspace_id,
            len(files),
        )
        return written

    async def sync_to_sandbox(self, sandbox, workspace_id: str) -> None:
        """同步指定 workspace 到沙箱（Local 后端用；E2B 后端 no-op），成功后清除 dirty 标记。"""
        await self._backend.sync_workspace_to_sandbox(workspace_id, sandbox)
        self._dirty.discard(workspace_id)

    async def ensure_volume(self, user_id: str, workspace_id: str) -> str | None:
        """确保工作区有持久化 Volume（E2B 懒创建），返回 volume_name；Local 返回 None。"""
        return await self._backend.ensure_volume(user_id, workspace_id)

    # ---- 方案 B：本地目录绑定（B1a）----

    async def load_and_bind_local_root(
        self, user_id: str, workspace_id: str
    ) -> str | None:
        """从 DB 读取 workspace 的 local_root 并注册到后端（仅 Local 后端）。

        注册后 LocalVolumeBackend 以该本地目录为存储根，读写直连，
        彻底绕开 workspace-volumes/{id}/ 副本。
        """
        if not isinstance(self._backend, LocalVolumeBackend):
            return None
        if workspace_id in self._local_root_loaded:
            return self.local_root_of(workspace_id)
        self._local_root_loaded.add(workspace_id)
        root = await self._store.get_local_root(user_id, workspace_id)
        if root:
            prefix = await self._store.get_local_root_prefix(user_id, workspace_id)
            self._backend.bind_local_root(workspace_id, root, prefix)
            logger.info(
                "workspace %s bound to local root %s (prefix=%r)",
                workspace_id,
                root,
                prefix,
            )
        return root

    async def bind_local_root(
        self,
        user_id: str,
        workspace_id: str,
        local_root: str,
        prefix: str = "",
        *,
        merge_existing: bool = False,
    ) -> BindOutcome:
        """给已有 workspace 绑定本地目录（方案 B）。

        绑定后该 workspace 读写直连 local_root。旧副本（workspace-volumes/{id}，即未绑定
        期间的产物）**默认不合并**——绑定一个既有目录不应附带全量文件迁移；调用方如需旧
        产物，显式传 ``merge_existing=True``（仍**只新增不覆盖**）。

        返回 BindOutcome：本次合并文件数 + 旧副本中仍未合并（本地缺失）的文件数，
        后者供 UI 提示「还有 N 个文件未合并」。
        """
        await self._store.bind_local_root(user_id, workspace_id, local_root, prefix)
        self._local_root_loaded.add(workspace_id)
        if not isinstance(self._backend, LocalVolumeBackend):
            return BindOutcome()
        self._backend.bind_local_root(workspace_id, local_root, prefix)
        from .sync import merge_dir_add_only, plan_merge

        legacy = self._backend.legacy_dir(workspace_id)
        dst = Path(local_root)
        merged = merge_dir_add_only(legacy, dst, prefix=prefix) if merge_existing else 0
        # 合并后重算：仍在旧副本、本地没有的文件（含合并失败项）
        unmerged = len(plan_merge(legacy, dst, prefix=prefix))
        return BindOutcome(merged_files=merged, unmerged_files=unmerged)

    def local_root_prefix_of(self, workspace_id: str) -> str:
        """返回已注册的本地目录前缀（未绑定返回 ""）。"""
        if not isinstance(self._backend, LocalVolumeBackend):
            return ""
        return self._backend.local_root_prefix_of(workspace_id)

    def local_root_of(self, workspace_id: str) -> str | None:
        """返回已注册的本地目录（未绑定返回 None）。"""
        if not isinstance(self._backend, LocalVolumeBackend):
            return None
        root = self._backend.local_root_of(workspace_id)
        return str(root) if root else None

    def is_dirty(self, workspace_id: str) -> bool:
        """该 workspace 是否有新写入、待同步到沙箱的文件。"""
        return workspace_id in self._dirty

    async def on_delete(
        self, user_id: str, workspace_id: str, volume_id: str | None
    ) -> None:
        """删除工作区时调用：销毁后端存储（E2B 按 volume_id，Local 按 workspace_id）。"""
        if volume_id:
            # E2B 后端：volume_id 即远端 Volume id
            try:
                await self._backend.destroy(user_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 - 销毁失败不影响删除
                logger.warning(
                    "failed to destroy volume %s: %s", volume_id, exc
                )
        else:
            # Local 后端：无 volume_id，按 workspace_id 清理本地目录
            await self._backend.destroy(user_id, workspace_id)

    async def destroy(self, user_id: str, workspace_id: str) -> None:
        """销毁工作区存储并清空绑定。"""
        await self._backend.destroy(user_id, workspace_id)
        await self._store.clear_volume(user_id, workspace_id)

    @staticmethod
    def is_safe_rel_path(rel_path: str) -> bool:
        """Volume 内相对路径安全校验：拒绝绝对路径与 .. 穿越。"""
        if not rel_path or rel_path.startswith("/"):
            return False
        return ".." not in rel_path.split("/")
