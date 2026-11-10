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


def _bind_workspace_segment(
    parts: list[str], index: int, current_workspace_id: str, label: str
) -> list[str]:
    """把命名空间里的 workspace_id 段绑定到当前会话的工作区。

    `workspace/` 与 `skills/project/` 都只有**一个**合法取值（当前会话的工作区），
    所以缺这一段时补全，而不是报错。原来的「缺 ID 段 → ValueError」看似更严，
    实际制造了一个死结：`skills/project` 报「必须补 workspace_id」，
    补上之后又因为调用点没带上下文报「没有 workspace 上下文」——
    两条要求互相矛盾，任何路径都走不通（文件面板展开节点、模型先 ls 再进都会撞上）。
    补全同样不能浏览其他项目：这段永远等于当前会话的工作区。

    唯一的硬拒绝是**没有上下文**：那时无法判断它指向哪个工作区。
    """
    if not current_workspace_id:
        raise PermissionError(f"{label} is not accessible without a workspace context")
    if len(parts) <= index:
        return [*parts, current_workspace_id]
    if parts[index] != current_workspace_id:
        raise PermissionError(
            f"{label} '{parts[index]}' is not accessible from this session "
            f"(current workspace: '{current_workspace_id}')"
        )
    return parts


def workspace_path(
    input_path: str, allow_root: bool, current_workspace_id: str = ""
) -> str:
    """把工作区相对路径转为 Sandbox 绝对路径，并拒绝越权路径。

    安全策略：
    - 路径必须相对 WORKSPACE.root
    - 禁止绝对路径（以 '/' 开头）
    - 禁止 '..' 跳出
    - 必须位于白名单子目录下：uploads/、work/、artifacts/、workspace/、skills/
    - `workspace/` 与 `skills/project/` 是**虚拟命名空间**，都绑定当前会话的工作区：
      缺 ID 段时补全为当前工作区（见 `_bind_workspace_segment`），
      ID 段与当前工作区不一致时拒绝（一个 session 只能访问自己的工作区）
    - **没有 workspace 上下文时，这两个命名空间一律拒绝**——此时项目级 Skill
      本来就不会被注入，放行只会让「同一用户跨项目读」多出一条不经过归属校验的路。
    - allow_root=True 时允许空路径/'.' 表示根目录（供 list_files 浏览根）
    """
    relative = input_path.strip().replace("\\", "/")
    if relative in ("", "."):
        if allow_root:
            return WORKSPACE.root
        raise ValueError("workspace root is not allowed here; provide a subpath")

    if relative.startswith("/"):
        raise ValueError("sandbox paths must be workspace-relative")

    # 尾随斜杠不影响语义（面板传 `skills/project/` 与 `skills/project` 等价），
    # 但会 split 出一个空段，让「缺 ID 段」的判断失灵（'' 被当成 ID 段 → 越权）。
    relative = relative.rstrip("/")

    parts = relative.split("/")
    if ".." in parts:
        raise ValueError("sandbox paths cannot contain '..'")

    # 第一段必须是允许的顶级目录
    top_dir = parts[0]
    if top_dir not in ALLOWED_BASE_DIRS:
        raise ValueError(f"top-level directory must be one of: {ALLOWED_BASE_DIRS}")

    # 两个虚拟命名空间：都只代表「当前会话的工作区」，规则完全相同。
    # 缺 ID 段 → 补全；有 ID 段 → 校验越权；无上下文 → 拒绝。
    if top_dir == "workspace":
        parts = _bind_workspace_segment(parts, 1, current_workspace_id, "workspace")
    elif top_dir == "skills" and parts[1:2] == ["project"]:
        parts = _bind_workspace_segment(parts, 2, current_workspace_id, "skills/project")

    return posixpath.join(WORKSPACE.root, *parts)


def relative_path(real_path: str) -> str:
    """把 Sandbox 绝对路径转回工作区相对路径。"""
    cleaned = posixpath.normpath(real_path)
    if cleaned == WORKSPACE.root:
        return "."
    prefix = WORKSPACE.root + "/"
    return cleaned.removeprefix(prefix)
