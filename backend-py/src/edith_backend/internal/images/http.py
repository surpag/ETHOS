"""images 的 HTTP 接口。

对应 Go 版 internal/images/http.go + routes.go。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from ..http_utils import EDITHError
from .service import Service
from .types import (
    CompleteUploadRequest,
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadInput,
)


def _image_dict(image) -> dict:
    return {"id": image.id, "mimeType": image.mime_type}


class HTTP:
    def __init__(self, service: Service) -> None:
        self._service = service
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/internal/images", response_model=CreateUploadResponse)
        async def create_upload(body: CreateUploadRequest) -> CreateUploadResponse:
            if not body.user_id.strip() or not body.session_id.strip() or not body.mime_type.strip() or body.size_bytes <= 0:
                raise EDITHError(400, "invalid_request", "userId, sessionId, mimeType, and sizeBytes are required")
            try:
                image, upload_url = await self._service.create_upload(
                    body.user_id,
                    UploadInput(session_id=body.session_id, mime_type=body.mime_type, size_bytes=body.size_bytes),
                )
            except ValueError as exc:
                raise EDITHError(400, "image_upload_rejected", str(exc))
            return CreateUploadResponse(image=_image_dict(image), upload_url=upload_url)

        @router.post("/internal/images/{image_id}/complete", response_model=CompleteUploadResponse)
        async def complete_upload(image_id: str, body: CompleteUploadRequest) -> CompleteUploadResponse:
            if not body.user_id.strip() or not image_id.strip():
                raise EDITHError(400, "invalid_request", "userId and imageID are required")
            try:
                image = await self._service.complete_upload(body.user_id, image_id)
            except ValueError as exc:
                raise EDITHError(400, "image_upload_rejected", str(exc))
            return CompleteUploadResponse(image=_image_dict(image))

        @router.get("/internal/images/{image_id}")
        async def open_image(image_id: str, userId: str = "") -> RedirectResponse:
            if not userId.strip() or not image_id.strip():
                raise EDITHError(400, "invalid_request", "userId and imageID are required")
            try:
                url = await self._service.open_for_user(userId, image_id)
            except Exception:  # noqa: BLE001 - 读取失败统一映射为 not_found
                raise EDITHError(404, "image_not_found", "image not found")
            return RedirectResponse(url=url, status_code=302, headers={"Cache-Control": "no-store"})

        return router
