"""sandbox 模块：按用户和会话隔离的 E2B 工作区及其 Agent 工具。

对应 Go 版 internal/sandbox。
"""

from .agent_input import AgentInput
from .http import HTTP
from .schema import create_schema
from .service import Service
from .tools import SandboxToolSet
from .workspace import WorkspaceLayout, relative_path, workspace_path

__all__ = [
    "HTTP",
    "AgentInput",
    "SandboxToolSet",
    "Service",
    "WorkspaceLayout",
    "create_schema",
    "relative_path",
    "workspace_path",
]
