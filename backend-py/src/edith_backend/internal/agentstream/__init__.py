"""agentstream 模块：渠道中性事件模型与流式解码器。

对应 Go 版 internal/agentstream。
Event 是所有渠道输出的唯一事件格式，13 个字段与 Go 版完全一致。
"""

from .decoder import StreamDecoder
from .types import APIError, Event, SessionUsage

__all__ = ["APIError", "Event", "SessionUsage", "StreamDecoder"]
