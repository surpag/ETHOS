"""sandbox 的路径安全校验。

对应 Go 版 internal/sandbox/workspace.go + files.go 的 workspacePath/relativePath。
目录约定（WorkspaceLayout / WORKSPACE）下沉到 workspace 包，这里只保留校验函数。
"""

from __future__ import annotations

import posixpath

from ..workspace.layout import (  # noqa: F401 - re-export 供既有 import 使用
    WORKSPACE,
    WorkspaceLayout,
)

# 允许访问的顶级目录白名单（公开：供 sandbox/tools.py 的路径自动补全复用，
# 避免 tools 重复定义导致白名单漂移。原为 _ALLOWED_BASE_DIRS，本次公开改名）
ALLOWED_BASE_DIRS = {"uploads", "work", "artifacts", "workspace", "skills"}


def workspace_path(
    input_path: str, allow_root: bool, current_workspace_id: str = ""
) -> str:
    """把工作区相对路径转为 Sandbox 绝对路径，并拒绝越权路径。

    安全策略：
    - 路径必须相对 WORKSPACE.root
    - 禁止绝对路径（以 '/' 开头）
    - 禁止 '..' 跳出
    - 必须位于白名单子目录下：uploads/、work/、artifacts/、workspace/、skills/
    - workspace/ 下的路径必须带有 workspace_id（不能直接访问 workspace/ 根）
    - **授权校验**：current_workspace_id 非空时，workspace/ 下的第二段必须等于它
      （一个 session 只能访问自己的工作区，不能读写其他工作区的文件）
    - allow_root=True 时允许空路径/'.' 表示根目录（供 list_files 浏览根）
    """
    relative = input_path.strip().replace("\\", "/")
    if relative in ("", "."):
        if allow_root:
            return WORKSPACE.root
        raise ValueError("workspace root is not allowed here; provide a subpath")

    if relative.startswith("/"):
        raise ValueError("sandbox paths must be workspace-relative")

    parts = relative.split("/")
    if ".." in parts:
        raise ValueError("sandbox paths cannot contain '..'")

    # 第一段必须是允许的顶级目录
    top_dir = parts[0]
    if top_dir not in ALLOWED_BASE_DIRS:
        raise ValueError(f"top-level directory must be one of: {ALLOWED_BASE_DIRS}")

    # 如果第一段是 workspace，第二段必须是 workspace_id（不能只到 workspace/）
    if top_dir == "workspace":
        if len(parts) < 2:
            raise ValueError(
                "workspace path must include workspace_id: workspace/{workspace_id}/..."
            )
        target_workspace_id = parts[1]
        if current_workspace_id and target_workspace_id != current_workspace_id:
            raise PermissionError(
                f"workspace '{target_workspace_id}' is not accessible from this session "
                f"(current workspace: '{current_workspace_id}')"
            )

    return posixpath.join(WORKSPACE.root, relative)


def relative_path(real_path: str) -> str:
    """把 Sandbox 绝对路径转回工作区相对路径。"""
    cleaned = posixpath.normpath(real_path)
    if cleaned == WORKSPACE.root:
        return "."
    prefix = WORKSPACE.root + "/"
    return cleaned.removeprefix(prefix)
