"""用户配置模块：设置、供应商密钥、渠道绑定与 MCP。

对应 Go 版 internal/userconfig。
"""

from .bindings import Bindings
from .http import HTTP
from .mcp import MCP
from .module import Module, new_module
from .providers import Providers
from .settings import Settings
from .types import (
    AgentSettings,
    ProviderStatus,
    SettingsRequest,
    SettingsResponse,
)

__all__ = [
    "HTTP",
    "MCP",
    "AgentSettings",
    "Bindings",
    "Module",
    "ProviderStatus",
    "Providers",
    "Settings",
    "SettingsRequest",
    "SettingsResponse",
    "new_module",
]
