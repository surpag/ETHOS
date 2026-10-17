"""HTTP 各模块共用的错误响应与 JSON 契约。

对应 Go 版 internal/httpx/json.go。
错误响应格式固定为 {"type": ..., "message": ...}，与 Go 版完全一致。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class EDITHError(Exception):
    """可转化为统一 JSON 错误响应的业务错误。

    对应 Go 版 httpx.WriteError 的 status + type + message 三元组。
    """

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


def register_error_handler(app: FastAPI) -> None:
    """注册统一错误处理，把 EDITHError 转为 {"type","message"} JSON 响应。"""

    @app.exception_handler(EDITHError)
    async def _edithe_handler(request: Request, exc: EDITHError) -> JSONResponse:
        logger.warning(
            "http error status=%d type=%s path=%s message=%s",
            exc.status_code, exc.error_type, request.url.path, exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": exc.error_type, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 请求体校验失败（含未知字段）统一为 400 invalid_request，
        # 对齐 Go 版 ReadJSON 的 DisallowUnknownFields 行为。
        logger.warning("http error status=400 type=invalid_request path=%s", request.url.path)
        return JSONResponse(
            status_code=400,
            content={"type": "invalid_request", "message": "invalid request"},
        )
