"""会话级并发控制：保证同一用户的同一会话一次只运行一个任务。

对应 Go 版 internal/agentrun/session_lanes.go。
asyncio 单线程事件循环下，dict 操作天然原子，无需显式加锁。
"""

from __future__ import annotations


class SessionLanes:
    def __init__(self) -> None:
        self.active: dict[tuple[str, str], str] = {}

    def acquire(self, user_id: str, session_id: str, request_id: str) -> bool:
        key = (user_id, session_id)
        if key in self.active:
            return False
        self.active[key] = request_id
        return True

    def release(self, user_id: str, session_id: str, request_id: str) -> None:
        key = (user_id, session_id)
        if self.active.get(key) == request_id:
            del self.active[key]
