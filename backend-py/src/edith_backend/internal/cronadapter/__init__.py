"""cronadapter 模块：把定时任务转换为 Gateway 消息。

对应 Go 版 internal/cronadapter。
"""

from .adapter import Adapter

__all__ = ["Adapter"]
