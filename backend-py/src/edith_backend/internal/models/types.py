"""模型目录的类型定义。

HTTP 契约用 Pydantic（camelCase 序列化对齐 Go 版 json tag），
内部定义用 dataclass（不可变，不参与序列化）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# 自定义条目的 ID 前缀：与内置 ID 空间不相交，合并时不需要任何「是否自定义」的判断。
CUSTOM_PROVIDER_PREFIX = "custom.provider:"
CUSTOM_MODEL_PREFIX = "custom.model:"


class ProviderInfo(BaseModel):
    """用户需要配置 API Key 的供应商（内置或用户自建）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: str
    base_url: str = ""
    custom: bool = False
    model_count: int = 0


class ReasoningOption(BaseModel):
    """模型专属的推理选项。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: str


class Capabilities(BaseModel):
    """模型支持的能力。"""

    vision: bool


class ModelInfo(BaseModel):
    """可安全发送给浏览器的模型描述。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    provider_id: str
    name: str
    reasoning_options: list[ReasoningOption] = field(default_factory=list)
    capabilities: Capabilities
    custom: bool = False
    context_window: int = 0


class CatalogResponse(BaseModel):
    """模型目录 HTTP 输出。"""

    providers: list[ProviderInfo]
    models: list[ModelInfo]


class AvailableResponse(BaseModel):
    """已配置凭据的模型列表 HTTP 输出。"""

    models: list[ModelInfo]


@dataclass(frozen=True)
class ModelDefinition:
    """模型目录的完整内部定义，绑定浏览器目录项与运行所需的 API 参数。

    context_window / api_model / base_url / extra_fields 仅供后端使用，
    不暴露给浏览器。
    """

    id: str
    provider_id: str
    name: str
    context_window: int
    api_model: str
    base_url: str
    vision: bool = False
    reasoning_options: tuple[ReasoningOption, ...] = ()
    extra_fields: dict[str, Any] = field(default_factory=dict)
    does_not_report_cached_prompt_tokens: bool = False

    def to_info(self) -> ModelInfo:
        """投影为浏览器可见的目录项。"""
        return ModelInfo(
            id=self.id,
            provider_id=self.provider_id,
            name=self.name,
            reasoning_options=list(self.reasoning_options),
            capabilities=Capabilities(vision=self.vision),
            custom=self.id.startswith(CUSTOM_MODEL_PREFIX),
            context_window=self.context_window,
        )


class CreateProviderRequest(BaseModel):
    """新增自定义供应商。apiKey 可后补，但为空时该供应商下的模型不可用于聊天。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str = ""
    name: str
    base_url: str
    api_key: str = ""


class CreateModelRequest(BaseModel):
    """在某个供应商下新增模型。

    contextWindow 必填：它决定历史摘要阈值与工具结果预算（execution.py），
    留空会让运行时退化成「每轮都压缩」，所以不给「未知」这个选项。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str = ""
    provider_id: str
    api_model: str
    name: str = ""
    context_window: int = 128_000
    vision: bool = False


class UpdateModelRequest(BaseModel):
    """更新自定义模型；未提供的字段保持不变。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str = ""
    name: str | None = None
    api_model: str | None = None
    context_window: int | None = None
    vision: bool | None = None


class DiscoverRequest(BaseModel):
    """拉取远端模型列表。providerId 已保存的供应商可省 baseUrl / apiKey。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str = ""
    provider_id: str = ""
    base_url: str = ""
    api_key: str = ""


class ProbeRequest(BaseModel):
    """连通性测试；同样支持「已保存供应商」与「未保存草稿」两种来源。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: str = ""
    api_model: str
    provider_id: str = ""
    base_url: str = ""
    api_key: str = ""


class DiscoveryResponse(BaseModel):
    """远端声明的模型名列表。"""

    models: list[str] = field(default_factory=list)


class ProbeResponse(BaseModel):
    """连通性测试结果。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    ok: bool
    latency_ms: int = 0

