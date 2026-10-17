"""工作区的目录约定（纯常量，不依赖任何业务模块）。

对应设计稿 P1：工作区文件挂载到 Sandbox 的固定路径。
独立成包是为了让 sandbox（中间层）依赖 workspace（底层），保持依赖漏斗。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceLayout:
    """每个 E2B Sandbox 的固定目录约定。"""

    root: str = "/home/user"
    uploads: str = "uploads"
    work: str = "work"
    artifacts: str = "artifacts"
    workspace: str = "workspace"

    def work_path(self) -> str:
        return posixpath.join(self.root, self.work)

    def workspace_path(self, workspace_id: str) -> str:
        """返回某工作区在 Sandbox 中的挂载路径（如 /home/user/workspace/{id}）。"""
        return posixpath.join(self.root, self.workspace, workspace_id)

    def tool_guide(self) -> str:
        return (
            f"Sandbox 根目录是 {self.root}。路径必须相对此目录；"
            f"{self.uploads}/ 放上传文件，{self.work}/ 放处理文件，"
            f"{self.artifacts}/ 放最终交付文件，{self.workspace}/ 放项目文件。"
            "不能使用绝对路径或 ..。"
        )


WORKSPACE = WorkspaceLayout()
