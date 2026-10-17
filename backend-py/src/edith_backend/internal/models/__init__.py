"""模型目录模块：EDITH 内置模型定义 + 用户自定义模型。

内置目录（常量）是只读权威源；用户自定义条目存在数据库，通过 ID 前缀与
内置 ID 空间隔离，合并后对下游是一份统一清单。
"""

from .catalog import (
    DEFAULT_MODEL_ID,
    Catalog,
    ModelDefinition,
    model_definitions,
    provider_definitions,
)
from .custom import CUSTOM_MODEL_PREFIX, CUSTOM_PROVIDER_PREFIX, CustomModels
from .http import HTTP
from .module import Module, new_module
from .schema import create_schema
from .types import (
    AvailableResponse,
    Capabilities,
    CatalogResponse,
    DiscoveryResponse,
    ModelInfo,
    ProbeResponse,
    ProviderInfo,
    ReasoningOption,
)

__all__ = [
    "CUSTOM_MODEL_PREFIX",
    "CUSTOM_PROVIDER_PREFIX",
    "DEFAULT_MODEL_ID",
    "HTTP",
    "AvailableResponse",
    "Capabilities",
    "Catalog",
    "CatalogResponse",
    "CustomModels",
    "DiscoveryResponse",
    "ModelDefinition",
    "ModelInfo",
    "Module",
    "ProbeResponse",
    "ProviderInfo",
    "ReasoningOption",
    "create_schema",
    "model_definitions",
    "new_module",
    "provider_definitions",
]
