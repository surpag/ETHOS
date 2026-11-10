"""agentrun 的核心类型定义。

对应 Go 版 internal/agentrun/types.go。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..agentstream.types import Event

if TYPE_CHECKING:
    from .run_log import RunEventLog


@dataclass
class Request:
    """已经完成身份与会话转换的执行输入。"""

    request_id: str
    user_id: str
    session_id: str
    message: str = ""
    image_ids: list[str] = field(default_factory=list)
    upload_paths: list[str] = field(default_factory=list)
    model_id: str = ""
    reasoning_option_id: str = ""
    workspace_id: str = ""


@dataclass
class Error:
    """描述任务启动、执行或控制失败。"""

    type: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "message": self.message}


@dataclass
class Stream:
    """AgentRun 的唯一输出；调用方必须消费到收到 None 哨兵。

    它不是一次性队列，而是一份**可重放的日志**（见 run_log.RunEventLog）：
    同一份日志可以被多个订阅者同时消费、也可以被中途加入的订阅者从某个
    seq 之后补发。这是「刷新页面还能实时看到执行过程」的前提——刷新 =
    旧订阅者消失、新订阅者带上 after 回来，事件本身一条都没丢。
    """

    log: RunEventLog

    def open(self, after: int = 0) -> AsyncIterator[Event | None]:
        """订阅事件流（先补发 after 之后的历史，再接实时）。"""
        return self.log.open(after)


@dataclass
class Status:
    """仍在管理的活跃任务状态。"""

    request_id: str
    status: str
