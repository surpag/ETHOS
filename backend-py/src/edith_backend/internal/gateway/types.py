"""gateway 的类型定义。

对应 Go 版 internal/gateway/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agentrun.types import Request

# 渠道常量
WEB_CHANNEL = "web"
CRON_CHANNEL = "cron"


@dataclass
class IncomingMessage:
    """Adapter 交给 Gateway 的渠道事实。"""

    channel: str
    external_user_id: str
    session_key: str = ""
    request_id: str = ""
    message: str = ""
    image_ids: list[str] = field(default_factory=list)
    upload_paths: list[str] = field(default_factory=list)
    model_id: str = ""
    reasoning_option_id: str = ""
    workspace_id: str = ""


def to_agentrun_request(msg: IncomingMessage, user_id: str, session_id: str) -> Request:
    """把渠道消息翻译为 AgentRun 的运行输入。"""
    return Request(
        request_id=msg.request_id,
        user_id=user_id,
        session_id=session_id,
        message=msg.message,
        image_ids=msg.image_ids,
        upload_paths=msg.upload_paths,
        model_id=msg.model_id,
        reasoning_option_id=msg.reasoning_option_id,
        workspace_id=msg.workspace_id,
    )
