"""images 服务：图片预留、上传确认与读取。

对应 Go 版 internal/images/service.go。
"""

from __future__ import annotations

from uuid import uuid4

from .cos import COSClient, COSConfig, TencentCOS
from .store import PENDING, READY, ImageRecord, Store
from .types import Image, UploadInput

MAX_IMAGE_BYTES = 10 << 20

_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp"}


def supported_mime_type(mime_type: str) -> bool:
    return mime_type in _SUPPORTED_MIME


def max_image_bytes() -> int:
    return MAX_IMAGE_BYTES


class Service:
    def __init__(self, store: Store, cos: COSClient | None) -> None:
        self._store = store
        self._cos = cos

    @classmethod
    def build(cls, store: Store, config: COSConfig) -> Service:
        cos: COSClient | None = None
        if config.configured():
            cos = TencentCOS(config)
        return cls(store, cos)

    async def create_upload(self, user_id: str, input_: UploadInput) -> tuple[Image, str]:
        user_id = user_id.strip()
        input_.session_id = input_.session_id.strip()
        input_.mime_type = input_.mime_type.lower().strip()
        if not user_id or not input_.session_id:
            raise ValueError("userID and sessionID are required")
        if not supported_mime_type(input_.mime_type):
            raise ValueError(f'unsupported image mime type "{input_.mime_type}"')
        if input_.size_bytes <= 0 or input_.size_bytes > MAX_IMAGE_BYTES:
            raise ValueError(f"image size must be between 1 and {MAX_IMAGE_BYTES} bytes")
        if self._cos is None:
            raise RuntimeError("COS is not configured")

        image = Image(id="img_" + uuid4().hex, mime_type=input_.mime_type)
        object_key = "chat-images/" + image.id
        record = ImageRecord(
            image=image, user_id=user_id, session_id=input_.session_id,
            object_key=object_key, size_bytes=input_.size_bytes, status=PENDING,
        )
        await self._store.insert(record)
        try:
            upload_url = await self._cos.sign_put(object_key)
        except Exception:
            await self._store.delete(image.id)
            raise
        return image, upload_url

    async def complete_upload(self, user_id: str, image_id: str) -> Image:
        record = await self._store.for_user(user_id, image_id, ready_only=False)
        if record.status == READY:
            return record.image
        if self._cos is None:
            raise RuntimeError("COS is not configured")
        try:
            obj = await self._cos.head(record.object_key)
        except Exception:
            await self._discard(record)
            raise
        if obj.size_bytes != record.size_bytes or (obj.mime_type and obj.mime_type != record.image.mime_type):
            await self._discard(record)
            raise ValueError("uploaded image does not match reservation")
        await self._store.mark_ready(record.image.id)
        return record.image

    async def open_for_user(self, user_id: str, image_id: str) -> str:
        record = await self._store.for_user(user_id, image_id, ready_only=True)
        if self._cos is None:
            raise RuntimeError("COS is not configured")
        return await self._cos.sign_get(record.object_key)

    async def open_for_session(self, user_id: str, session_id: str, image_id: str) -> str:
        record = await self._store.for_session(user_id, session_id, image_id)
        if self._cos is None:
            raise RuntimeError("COS is not configured")
        return await self._cos.sign_get(record.object_key)

    async def _discard(self, record: ImageRecord) -> None:
        if self._cos is not None:
            try:
                await self._cos.delete(record.object_key)
            except Exception:  # noqa: BLE001, S110 - 清理失败不影响记录删除
                pass
        await self._store.delete(record.image.id)
