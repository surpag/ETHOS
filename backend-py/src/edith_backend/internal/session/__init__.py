"""session 模块：会话历史存储。

对应 Go 版由 trpc-agent-go 的 session service 承担的角色。
方案 B 自行用 SQLite 持久化对话消息，供 AgentRun 拼装上下文与 conversation 投影 Timeline。
"""

from .schema import create_schema
from .store import SessionStore
from .types import MessageRecord

__all__ = ["MessageRecord", "SessionStore", "create_schema"]
