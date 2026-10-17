"""工作区级互斥租约：保证同一用户的同一工作区同一时刻只被一个会话使用。

对应 Go 版 internal/agentrun/session_lanes.go 的同族机制。
asyncio 单线程事件循环下，dict 操作天然原子，无需显式加锁。

为什么需要（正确性要求，不是产品偏好）：
一个 workspace 绑定本地目录后，`双写 / B2 / B3` 都以「本地目录 ↔ 沙箱」为镜像轴。
若两个 session 同时使用同一 workspace，两个沙箱会互相覆盖对方与本地目录的内容
（后同步者胜，先同步者的改动被静默丢弃）。因此必须互斥。

与「严格绑定一个 session」的区别（乙方案）：
只互斥**同时**使用；历史会话只要不并发，仍然可以顺序使用同一工作区，
不破坏已有数据。
"""

from __future__ import annotations


class WorkspaceLanes:
    def __init__(self) -> None:
        self.active: dict[tuple[str, str], str] = {}

    def acquire(self, user_id: str, workspace_id: str, session_id: str) -> bool:
        """尝试持有工作区租约；被**其他**会话占用时返回 False（同会话可重入）。"""
        key = (user_id, workspace_id)
        holder = self.active.get(key)
        if holder is not None and holder != session_id:
            return False
        self.active[key] = session_id
        return True

    def release(self, user_id: str, workspace_id: str, session_id: str) -> None:
        key = (user_id, workspace_id)
        if self.active.get(key) == session_id:
            del self.active[key]

    def holder(self, user_id: str, workspace_id: str) -> str | None:
        """当前持有者 session_id（未持有返回 None），用于错误提示。"""
        return self.active.get((user_id, workspace_id))
