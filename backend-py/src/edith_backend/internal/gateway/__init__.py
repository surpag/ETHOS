"""gateway 模块：所有 Agent 渠道的统一入口。

对应 Go 版 internal/gateway。
"""

from .service import Service, new_service
from .types import IncomingMessage

__all__ = ["IncomingMessage", "Service", "new_service"]
