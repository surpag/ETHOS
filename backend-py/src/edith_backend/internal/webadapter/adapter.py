"""webadapter 适配器：HTTP/SSE 协议 → Gateway 调用。

对应 Go 版 internal/webadapter/adapter.go + stream.go + control.go。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..agentrun.types import Error
from ..agentstream.types import Event
from ..gateway.service import Service as GatewayService
from ..gateway.types import WEB_CHANNEL, IncomingMessage
from ..http_utils import EDITHError
from .types import MessageRequest


def _gateway_error_to_http(error: Error) -> EDITHError:
    """把 agentrun.Error 映射为 HTTP 状态码与统一错误响应。"""
    status = 500
    if error.type == "invalid_request":
        status = 400
    elif error.type in ("session_busy", "request_conflict"):
        status = 409
    elif error.type == "identity_not_bound":
        status = 403
    elif error.type == "not_found":
        status = 404
    return EDITHError(status, error.type, error.message)


class Adapter:
    """Web 渠道入口，只理解 HTTP、JSON 和 SSE。"""

    def __init__(self, gateway: GatewayService) -> None:
        self.gateway = gateway
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/internal/gateway/messages:stream")
        async def stream_message(body: MessageRequest) -> StreamingResponse:
            stream, run_error = await self.gateway.run(
                IncomingMessage(
                    channel=WEB_CHANNEL,
                    external_user_id=body.user_id,
                    session_key=body.session_id,
                    request_id=body.request_id,
                    message=body.message,
                    image_ids=body.image_ids,
                    upload_paths=body.upload_paths,
                    model_id=body.model_id,
                    reasoning_option_id=body.reasoning_option_id,
                    workspace_id=body.workspace_id,
                )
            )
            if run_error is not None:
                raise _gateway_error_to_http(run_error)

            async def event_generator() -> AsyncIterator[str]:
                while True:
                    event: Event | None = await stream.events.get()
                    if event is None:
                        break
                    # by_alias=True 输出 camelCase（assistantId/blockId/toolCallId…），
                    # 与 Go 版 agentstream.Event 的 json tag 保持一致，前端按 camelCase 读取。
                    # 序列化Event，按SSE格式输出
                    yield f"data: {event.model_dump_json(exclude_none=True, by_alias=True)}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )

        @router.get("/internal/gateway/runs/{request_id}")
        async def run_status(request_id: str, userId: str = "") -> dict:
            status, run_error = self.gateway.status(userId, request_id)
            if run_error is not None:
                raise _gateway_error_to_http(run_error)
            return {"requestId": status.request_id, "status": status.status}

        @router.post("/internal/gateway/runs/{request_id}/cancel")
        async def cancel_run(request_id: str, userId: str = "") -> None:
            run_error = self.gateway.cancel(userId, request_id)
            if run_error is not None:
                raise _gateway_error_to_http(run_error)

        return router
