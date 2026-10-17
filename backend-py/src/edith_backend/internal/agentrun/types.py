"""agentrun 的核心类型定义。

对应 Go 版 internal/agentrun/types.go。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..agentstream.types import Event


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

    events 队列中的 None 表示事件流结束（对应 Go 版 channel 关闭）。
    """

    events: asyncio.Queue[Event | None]


@dataclass
class Status:
    """仍在管理的活跃任务状态。"""

    request_id: str
    status: str
