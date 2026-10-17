"""阶段 7 验收测试：images 模块的校验与标记转换。"""

from __future__ import annotations

from edith_backend.internal.images.agent_input import image_id_from_reference, reference
from edith_backend.internal.images.cos import COSConfig
from edith_backend.internal.images.service import (
    Service,
    max_image_bytes,
    supported_mime_type,
)
from edith_backend.internal.images.store import Store


def test_supported_mime_types() -> None:
    assert supported_mime_type("image/jpeg")
    assert supported_mime_type("image/png")
    assert supported_mime_type("image/webp")
    assert not supported_mime_type("image/gif")
    assert not supported_mime_type("text/plain")


def test_reference_roundtrip() -> None:
    marker = reference("img_abc")
    assert marker == "edith-image://img_abc"
    image_id, ok = image_id_from_reference(marker)
    assert ok
    assert image_id == "img_abc"


def test_image_id_from_reference_rejects_plain() -> None:
    _image_id, ok = image_id_from_reference("img_abc")
    assert not ok


def test_cos_config_placeholder_not_configured() -> None:
    config = COSConfig(bucket="your-cos-bucket", region="ap-guangzhou",
                       secret_id="your-secret-id", secret_key="your-secret-key")
    assert config.configured() is False


def test_cos_config_real_configured() -> None:
    config = COSConfig(bucket="my-bucket-123", region="ap-guangzhou",
                       secret_id="AKIDxxxx", secret_key="secret")
    assert config.configured() is True


def test_max_image_bytes() -> None:
    assert max_image_bytes() == 10 << 20


def test_create_upload_rejects_unsupported_mime() -> None:
    import asyncio

    from edith_backend.internal.images.types import UploadInput

    service = Service(Store(None), None)
    try:
        asyncio.run(service.create_upload("u", UploadInput("s", "image/gif", 100)))
        assert False, "should reject unsupported mime"
    except ValueError as exc:
        assert "unsupported image mime" in str(exc)
