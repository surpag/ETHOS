"""conversation 模块：把会话历史投影为浏览器 Timeline。

对应 Go 版 internal/conversation。
"""

from .http import HTTP
from .module import Module, new_module
from .types import (
    AssistantBlock,
    AssistantContentBlock,
    Conversation,
    ConversationListResponse,
    ConversationResponse,
    ErrorBlock,
    Timeline,
    UserBlock,
    UserImage,
)

__all__ = [
    "HTTP",
    "AssistantBlock",
    "AssistantContentBlock",
    "Conversation",
    "ConversationListResponse",
    "ConversationResponse",
    "ErrorBlock",
    "Module",
    "Timeline",
    "UserBlock",
    "UserImage",
    "new_module",
]
