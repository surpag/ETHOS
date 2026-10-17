"""用户设置与供应商密钥的 HTTP 边界。

对应 Go 版 internal/userconfig/settings_http.go + routes.go。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..http_utils import EDITHError
from .gitcredentials import GitCredentials
from .providers import Providers
from .settings import Settings
from .types import (
    AgentSettings,
    GitCredentialRequest,
    GitCredentialsResponse,
    ProviderCredentialState,
    SettingsRequest,
    SettingsResponse,
)


class HTTP:
    """userconfig 对 Web BFF 公开的 HTTP 能力（对应 Go 版 settings_http.go 的 HTTP struct）。"""

    def __init__(
        self,
        settings: Settings,
        providers: Providers,
        git_credentials: GitCredentials,
        default_model_id: str,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.git_credentials = git_credentials
        self.default_model_id = default_model_id
        self.router = self._build_router()

    async def _write_settings(self, user_id: str) -> SettingsResponse:
        agent_settings = await self.settings.load(user_id)
        statuses = await self.providers.list_statuses(user_id)
        provider_states = [
            ProviderCredentialState(
                provider_id=status.provider_id,
                has_api_key=status.has_api_key,
            )
            for status in statuses
        ]
        model_id = agent_settings.default_model_id or self.default_model_id
        return SettingsResponse(
            personality=agent_settings.personality,
            default_model_id=model_id,
            timezone=agent_settings.timezone,
            git_author_name=agent_settings.git_author_name,
            git_author_email=agent_settings.git_author_email,
            providers=provider_states,
        )

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/user-settings", response_model=SettingsResponse)
        async def get_settings(userId: str = "") -> SettingsResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            return await self._write_settings(userId)

        @router.put("/internal/user-settings", response_model=SettingsResponse)
        async def save_settings(body: SettingsRequest) -> SettingsResponse:
            user_id = body.user_id.strip()
            model_id = body.default_model_id.strip()
            if not user_id or not model_id:
                raise EDITHError(400, "invalid_request", "userId and defaultModelId are required")

            # 供应商密钥：api_key 为空字符串视为「不覆盖」，转为 None。
            credentials: list[tuple[str, str | None]] = []
            for provider in body.providers:
                provider_id = provider.provider_id.strip()
                if not provider_id:
                    raise EDITHError(400, "invalid_request", "providerId is required")
                api_key = provider.api_key
                if api_key is not None:
                    api_key = api_key.strip() or None
                credentials.append((provider_id, api_key))

            await self.settings.save(
                user_id,
                AgentSettings(
                    personality=body.personality.strip(),
                    default_model_id=model_id,
                    timezone=body.timezone.strip(),
                    git_author_name=body.git_author_name.strip(),
                    git_author_email=body.git_author_email.strip(),
                ),
            )
            await self.providers.save(user_id, credentials)
            return await self._write_settings(user_id)

        @router.get("/internal/git-credentials", response_model=GitCredentialsResponse)
        async def get_git_credentials(userId: str = "") -> GitCredentialsResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            connected = await self.git_credentials.list_connected(userId)
            return GitCredentialsResponse(connected=connected)

        @router.put("/internal/git-credentials", response_model=GitCredentialsResponse)
        async def save_git_credential(body: GitCredentialRequest) -> GitCredentialsResponse:
            user_id = body.user_id.strip()
            if not user_id:
                raise EDITHError(400, "invalid_request", "userId is required")
            try:
                await self.git_credentials.save(user_id, body.provider, body.token)
            except ValueError as exc:
                raise EDITHError(400, "invalid_request", str(exc)) from exc
            connected = await self.git_credentials.list_connected(user_id)
            return GitCredentialsResponse(connected=connected)

        @router.delete("/internal/git-credentials", response_model=GitCredentialsResponse)
        async def delete_git_credential(
            userId: str = "", provider: str = "github"
        ) -> GitCredentialsResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            await self.git_credentials.delete(userId, provider)
            connected = await self.git_credentials.list_connected(userId)
            return GitCredentialsResponse(connected=connected)

        return router
