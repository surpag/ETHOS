"""workspace 模块：工作区实体、指令、自动记忆与 Volume 持久化。

依赖方向（对齐依赖漏斗）：workspace 是底层模块，只依赖 e2b / aiosqlite，
不依赖 sandbox 等中间层；sandbox 反向依赖 workspace 的 layout 约定。
"""

from .http import (
    build_instruction_router,
    build_memory_router,
    build_workspace_router,
)
from .layout import WORKSPACE, WorkspaceLayout
from .memory_store import WorkspaceMemoryStore
from .schema import create_schema
from .store import DuplicateWorkspace, WorkspaceStore
from .volume_backend import new_backend
from .volume_service import WorkspaceVolumeService

__all__ = [
    "WORKSPACE",
    "DuplicateWorkspace",
    "WorkspaceLayout",
    "WorkspaceMemoryStore",
    "WorkspaceStore",
    "WorkspaceVolumeService",
    "build_instruction_router",
    "build_memory_router",
    "build_workspace_router",
    "create_schema",
    "new_backend",
]
