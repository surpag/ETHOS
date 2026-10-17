"""images 的类型定义。

对应 Go 版 internal/images/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


@dataclass
class Image:
    """浏览器和 Agent 可见的持久图片身份。"""

    id: str
    mime_type: str


@dataclass
class UploadInput:
    session_id: str
    mime_type: str
    size_bytes: int


class CreateUploadRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    user_id: str = ""
    session_id: str = ""
    mime_type: str = ""
    size_bytes: int = 0


class CreateUploadResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    image: dict
    upload_url: str


class CompleteUploadRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    user_id: str = ""


class CompleteUploadResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    image: dict
