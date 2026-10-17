"""COS 客户端接口与腾讯云实现。

对应 Go 版 internal/images/cos.go。
用接口抽象 signPut/signGet/head/delete，便于测试 mock 与无配置降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

SIGNED_URL_LIFETIME = 5 * 60  # 5 分钟


@dataclass
class ObjectInfo:
    mime_type: str
    size_bytes: int


class COSClient(Protocol):
    async def sign_put(self, key: str) -> str: ...
    async def sign_get(self, key: str) -> str: ...
    async def head(self, key: str) -> ObjectInfo: ...
    async def delete(self, key: str) -> None: ...


@dataclass
class COSConfig:
    bucket: str = ""
    region: str = ""
    secret_id: str = ""
    secret_key: str = ""

    def configured(self) -> bool:
        def real(value: str) -> bool:
            value = value.strip()
            return bool(value) and not value.lower().startswith("your-")

        return all(real(v) for v in (self.bucket, self.region, self.secret_id, self.secret_key))


class TencentCOS:
    """基于 cos-python-sdk-v5 的腾讯云 COS 实现。"""

    def __init__(self, config: COSConfig) -> None:
        from qcloud_cos import CosConfig, CosS3Client

        self._config = config
        self._client = CosS3Client(
            CosConfig(
                Region=config.region,
                SecretId=config.secret_id,
                SecretKey=config.secret_key,
            )
        )

    async def sign_put(self, key: str) -> str:
        return self._client.get_presigned_url(
            Method="PUT", Bucket=self._config.bucket, Key=key, Expired=SIGNED_URL_LIFETIME
        )

    async def sign_get(self, key: str) -> str:
        return self._client.get_presigned_url(
            Method="GET", Bucket=self._config.bucket, Key=key, Expired=SIGNED_URL_LIFETIME
        )

    async def head(self, key: str) -> ObjectInfo:
        raise NotImplementedError("COS head not implemented without configured credentials")

    async def delete(self, key: str) -> None:
        raise NotImplementedError("COS delete not implemented without configured credentials")
