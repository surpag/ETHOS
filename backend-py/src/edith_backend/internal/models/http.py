"""模型目录的 HTTP 边界：内置目录查询 + 自定义供应商/模型的增删改查。

userId 由 BFF 注入（与 `gitops/http.py` 的 PushRequest 一致）。不传 userId 的
读接口只返回内置目录，因此 IM、定时任务这类无用户上下文的入口不会受影响。

业务校验失败（URL 非法、模型名非法、上下文窗口越界、试图改动内置条目）
统一翻译成 400 `invalid_request`：这些都是「用户填错了」而不是服务端故障，
不该产生 5xx 告警。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from ..http_utils import EDITHError
from .catalog import Catalog
from .custom import CustomModels
from .endpoint import (
    EndpointRejected,
    discover_models,
    normalize_endpoint_url,
    probe_model,
)
from .types import (
    AvailableResponse,
    CatalogResponse,
    CreateModelRequest,
    CreateProviderRequest,
    DiscoverRequest,
    DiscoveryResponse,
    ModelInfo,
    ProbeRequest,
    ProbeResponse,
    ProviderInfo,
    UpdateModelRequest,
)

if TYPE_CHECKING:
    from ..userconfig.providers import Providers


class HTTP:
    """models 对 Web BFF 公开的 HTTP 能力（对应 Go 版 http.go 的 HTTP struct）。"""

    def __init__(
        self, catalog: Catalog, providers: Providers, custom: CustomModels | None = None
    ) -> None:
        self.catalog = catalog
        self.providers = providers
        self.custom = custom
        self.router = self._build_router()

    def _require_custom(self) -> CustomModels:
        if self.custom is None:
            raise EDITHError(503, "unavailable", "custom model storage is not configured")
        return self.custom

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/models", response_model=CatalogResponse)
        async def list_catalog(userId: str = "") -> CatalogResponse:
            return CatalogResponse(
                providers=await self.catalog.all_providers(userId.strip()),
                models=await self.catalog.all_models(userId.strip()),
            )

        @router.get("/internal/available-models", response_model=AvailableResponse)
        async def list_available(userId: str = "") -> AvailableResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            statuses = await self.providers.list_statuses(userId)
            has_key = {status.provider_id: status.has_api_key for status in statuses}
            available: list[ModelInfo] = [
                definition.to_info()
                for definition in await self.catalog.all_definitions(userId)
                if has_key.get(definition.provider_id, False)
            ]
            return AvailableResponse(models=available)

        @router.post("/internal/custom-providers", response_model=ProviderInfo)
        async def create_provider(body: CreateProviderRequest) -> ProviderInfo:
            custom = self._require_custom()
            user_id = _require_user(body.user_id)
            try:
                return await custom.create_provider(
                    user_id, body.name, body.base_url, body.api_key
                )
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc

        @router.delete("/internal/custom-providers/{provider_id}")
        async def delete_provider(provider_id: str, userId: str = "") -> dict:
            custom = self._require_custom()
            user_id = _require_user(userId)
            try:
                await custom.delete_provider(user_id, provider_id)
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc
            return {"ok": True}

        @router.post("/internal/custom-models", response_model=ModelInfo)
        async def create_model(body: CreateModelRequest) -> ModelInfo:
            custom = self._require_custom()
            user_id = _require_user(body.user_id)
            base_url = await self.catalog.resolve_base_url(user_id, body.provider_id.strip())
            if not base_url:
                raise EDITHError(400, "invalid_request", "unknown provider")
            try:
                definition = await custom.create_model(
                    user_id,
                    body.provider_id.strip(),
                    body.api_model,
                    base_url,
                    body.name,
                    body.context_window,
                    body.vision,
                )
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc
            return definition.to_info()

        # 静态子路径必须注册在 `{model_id}` 之前：FastAPI 按注册顺序匹配，
        # 否则未来再加 POST /custom-models/<静态名> 会被 {model_id} 模板吞成 405。
        @router.post("/internal/custom-models/discover", response_model=DiscoveryResponse)
        async def discover(body: DiscoverRequest) -> DiscoveryResponse:
            self._require_custom()  # 未装配自定义存储时直接 503
            user_id = _require_user(body.user_id)
            base_url, api_key = await self._resolve_target(
                user_id, body.provider_id, body.base_url, body.api_key
            )
            try:
                return DiscoveryResponse(models=await discover_models(base_url, api_key))
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc

        @router.post("/internal/custom-models/probe", response_model=ProbeResponse)
        async def probe(body: ProbeRequest) -> ProbeResponse:
            self._require_custom()  # 未装配自定义存储时直接 503
            user_id = _require_user(body.user_id)
            base_url, api_key = await self._resolve_target(
                user_id, body.provider_id, body.base_url, body.api_key
            )
            try:
                result = await probe_model(base_url, api_key, body.api_model)
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc
            return ProbeResponse(**result)

        @router.patch("/internal/custom-models/{model_id}")
        async def update_model(model_id: str, body: UpdateModelRequest) -> dict:
            custom = self._require_custom()
            user_id = _require_user(body.user_id)
            try:
                await custom.update_model(
                    user_id,
                    model_id,
                    name=body.name,
                    api_model=body.api_model,
                    context_window=body.context_window,
                    vision=body.vision,
                )
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc
            return {"ok": True}

        @router.delete("/internal/custom-models/{model_id}")
        async def delete_model(model_id: str, userId: str = "") -> dict:
            custom = self._require_custom()
            user_id = _require_user(userId)
            try:
                await custom.delete_model(user_id, model_id)
            except EndpointRejected as exc:
                raise _bad_request(exc) from exc
            return {"ok": True}

        return router

    async def _resolve_target(
        self,
        user_id: str,
        provider_id: str,
        base_url: str,
        api_key: str,
    ) -> tuple[str, str]:
        """解析 discover / probe 的目标端点与凭据。

        已保存的供应商只需要 providerId —— 凭据在库里，浏览器不必（也不该）
        把 Key 回传；未保存的草稿则直接带 baseUrl + apiKey。二者择一，
        避免「前端悄悄覆盖库里的值」这种很难排查的状态。
        """
        provider_id = provider_id.strip()
        api_key = api_key.strip()
        if provider_id:
            resolved_url = await self.catalog.resolve_base_url(user_id, provider_id)
            if not resolved_url:
                raise EDITHError(400, "invalid_request", "unknown provider")
            return resolved_url, api_key or await self.providers.try_load_api_key(
                user_id, provider_id
            )
        base_url = base_url.strip()
        if not base_url:
            raise EDITHError(400, "invalid_request", "providerId or baseUrl is required")
        try:
            return normalize_endpoint_url(base_url), api_key
        except EndpointRejected as exc:
            raise _bad_request(exc) from exc


def _require_user(user_id: str) -> str:
    if not (user_id or "").strip():
        raise EDITHError(400, "invalid_request", "userId is required")
    return user_id.strip()


def _bad_request(exc: EndpointRejected) -> EDITHError:
    return EDITHError(400, "invalid_request", str(exc))
