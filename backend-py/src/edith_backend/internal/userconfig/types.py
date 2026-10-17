"""用户配置模块的类型定义。

内部类型用 dataclass，HTTP 契约用 Pydantic（camelCase 对齐 Go 版 json tag）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


@dataclass
class AgentSettings:
    """用户可编辑的 Agent 配置，不含任何密钥。"""

    personality: str = ""
    default_model_id: str = ""
    timezone: str = ""
    # git 提交身份；留空则回退平台默认（EDITH_GIT_AUTHOR_NAME/EMAIL）
    git_author_name: str = ""
    git_author_email: str = ""


@dataclass
class ProviderStatus:
    """可安全返回浏览器的供应商配置状态。"""

    provider_id: str
    has_api_key: bool


class ProviderCredentialInput(BaseModel):
    """浏览器提交的供应商密钥；api_key 为空表示不覆盖已有密钥。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    provider_id: str
    api_key: str | None = None


class SettingsRequest(BaseModel):
    """保存用户设置的 HTTP 输入。userId 由 BFF 注入。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    personality: str = ""
    default_model_id: str = ""
    timezone: str = ""
    git_author_name: str = ""
    git_author_email: str = ""
    providers: list[ProviderCredentialInput] = Field(default_factory=list)


class ProviderCredentialState(BaseModel):
    """表示一个供应商是否已保存密钥。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    provider_id: str
    has_api_key: bool


class SettingsResponse(BaseModel):
    """读取或保存用户设置的 HTTP 输出，永不包含密钥。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    personality: str
    default_model_id: str
    timezone: str
    git_author_name: str = ""
    git_author_email: str = ""
    providers: list[ProviderCredentialState]


class GitCredentialRequest(BaseModel):
    """浏览器提交的 Git 凭据（PAT）。userId 由 BFF 注入。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    provider: str = "github"
    token: str


class GitCredentialsResponse(BaseModel):
    """Git 凭据状态输出：只暴露「哪些 provider 已连接」，永不包含 token。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    connected: list[str] = Field(default_factory=list)

