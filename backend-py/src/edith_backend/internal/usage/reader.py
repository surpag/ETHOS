"""usage 读取器：读取其他模块展示所需的用量数据。

对应 Go 版 internal/usage/reader.go。
"""

from __future__ import annotations

from .store import Store
from .types import Summary


class Reader:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def status(self, user_id: str, request_id: str) -> str:
        """返回该用户指定运行的持久化状态。"""
        return await self._store.status(user_id, request_id)

    async def session_summary(self, user_id: str, session_id: str) -> Summary:
        """返回一个会话的服务器计算用量汇总。"""
        return await self._store.session_summary(user_id, session_id)
