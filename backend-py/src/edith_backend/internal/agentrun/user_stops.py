"""用户主动停止标记：记录哪些任务由用户停止，用于输出 run.canceled。

对应 Go 版 internal/agentrun/user_stops.go。
"""

from __future__ import annotations


class UserStops:
    def __init__(self) -> None:
        self.request_ids: set[str] = set()

    def mark(self, request_id: str) -> None:
        self.request_ids.add(request_id)

    def contains(self, request_id: str) -> bool:
        return request_id in self.request_ids

    def take(self, request_id: str) -> bool:
        if request_id in self.request_ids:
            self.request_ids.discard(request_id)
            return True
        return False
