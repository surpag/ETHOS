"""webadapter 模块：把 Web BFF 的 HTTP/SSE 协议适配为 Gateway 调用。

对应 Go 版 internal/webadapter。
"""

from .adapter import Adapter
from .types import MessageRequest

__all__ = ["Adapter", "MessageRequest"]
