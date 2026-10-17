"""自定义模型（设置页填写模型名即可用）的验收测试。

覆盖三块：
1. 端点校验与模型名校验（纯函数，不发网络）
2. CRUD 全链路 + 与内置目录的合并语义（走 HTTP，userId 隔离数据）
3. 两条容易被忽略的护栏：内置条目只读、默认模型失效可回退
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from edith_backend.internal.agentrun.configuration import RunConfigurations
from edith_backend.internal.agentrun.types import Error, Request
from edith_backend.internal.models.catalog import DEFAULT_MODEL_ID, Catalog
from edith_backend.internal.models.endpoint import (
    EndpointRejected,
    normalize_endpoint_url,
    validate_api_model,
)
from edith_backend.internal.trace.tracer import Tracer
from edith_backend.internal.trace.types import Trace

_BUILTIN_MODEL_ID = "deepseek.v4.flash"


# ── 1. 端点与模型名校验 ────────────────────────────────────────────────


def test_endpoint_url_public_https_is_normalized() -> None:
    assert normalize_endpoint_url("https://api.moonshot.cn/v1/") == "https://api.moonshot.cn/v1"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/v1",
        "https://192.168.1.10/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost:8080/v1",
        "https://metadata.google.internal/v1",
    ],
)
def test_endpoint_url_rejects_private_and_metadata(url: str) -> None:
    with pytest.raises(EndpointRejected):
        normalize_endpoint_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "api.moonshot.cn/v1",
        "ftp://api.moonshot.cn/v1",
        "https://api.moonshot.cn/v1?key=1",
        "https://user:pass@api.moonshot.cn/v1",
    ],
)
def test_endpoint_url_rejects_malformed(url: str) -> None:
    with pytest.raises(EndpointRejected):
        normalize_endpoint_url(url)


def test_endpoint_url_allows_http_only_when_local_switch_on(monkeypatch) -> None:
    with pytest.raises(EndpointRejected):
        normalize_endpoint_url("http://localhost:11434/v1")
    monkeypatch.setenv("EDITH_ALLOW_LOCAL_MODEL_ENDPOINT", "true")
    assert normalize_endpoint_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    # 开关打开后仍拒绝云元数据：否则本地部署就成了读云凭据的跳板
    with pytest.raises(EndpointRejected):
        normalize_endpoint_url("http://169.254.169.254/v1")


@pytest.mark.parametrize("name", ["", "  ", "qwen 3.8", "a" * 129, "bad\nname"])
def test_api_model_validation_rejects(name: str) -> None:
    with pytest.raises(EndpointRejected):
        validate_api_model(name)


def test_api_model_validation_accepts_provider_style_ids() -> None:
    assert validate_api_model(" moonshot-v1-128k ") == "moonshot-v1-128k"
    assert validate_api_model("openai/gpt-4o") == "openai/gpt-4o"


# ── 2. CRUD 与合并语义 ────────────────────────────────────────────────


def _create_provider(client: TestClient, user_id: str, name: str = "Moonshot") -> dict:
    response = client.post(
        "/internal/custom-providers",
        json={
            "userId": user_id,
            "name": name,
            "baseUrl": "https://api.moonshot.cn/v1",
            "apiKey": "sk-test-key",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_model(client: TestClient, user_id: str, provider_id: str, **overrides) -> dict:
    body = {
        "userId": user_id,
        "providerId": provider_id,
        "apiModel": "moonshot-v1-128k",
        "name": "Moonshot 128k",
        "contextWindow": 128_000,
    }
    body.update(overrides)
    return client.post("/internal/custom-models", json=body)


def test_custom_model_appears_in_catalog_and_available(client: TestClient) -> None:
    user = "test-custom-flow"
    provider = _create_provider(client, user)
    assert provider["custom"] is True

    response = _create_model(client, user, provider["id"])
    assert response.status_code == 200, response.text
    model = response.json()
    assert model["custom"] is True
    assert model["contextWindow"] == 128_000

    catalog = client.get("/internal/models", params={"userId": user}).json()
    assert any(item["id"] == provider["id"] for item in catalog["providers"])
    assert any(item["id"] == model["id"] for item in catalog["models"])
    # 内置目录仍然完整（合并而不是替换）
    assert any(item["id"] == _BUILTIN_MODEL_ID for item in catalog["models"])

    # Key 存在 user_providers 里 → 该模型自动进入聊天页可选列表
    available = client.get("/internal/available-models", params={"userId": user}).json()
    assert model["id"] in {item["id"] for item in available["models"]}


def test_builtin_provider_custom_model_reaches_chat_picker(client: TestClient) -> None:
    """内置供应商下新增的模型，配好 Key 后必须出现在聊天页可选列表里。"""
    user = "test-custom-builtin-available"
    saved = client.put(
        "/internal/user-settings",
        json={
            "userId": user,
            "defaultModelId": _BUILTIN_MODEL_ID,
            "providers": [{"providerId": "deepseek", "apiKey": "sk-test-key"}],
        },
    )
    assert saved.status_code == 200, saved.text

    created = _create_model(client, user, "deepseek", apiModel="deepseek-v4.1-nitro").json()
    available = client.get("/internal/available-models", params={"userId": user}).json()
    ids = {item["id"] for item in available["models"]}
    assert created["id"] in ids
    # 同供应商的内置模型也还在（合并而不是替换）
    assert _BUILTIN_MODEL_ID in ids


def test_probe_and_discover_routes_are_reachable(client: TestClient) -> None:
    """这两个路由必须真的存在。

    曾经的 bug：BFF 把 `test` 直接拼进路径（`/internal/custom-models/test`），
    命中了 `{modelId}` 的路径模板 → 405，前端只显示「连接失败」。
    这里断言「不是 405」，缺凭据时应是 400 —— 说明请求确实到达了处理函数。
    """
    user = "test-custom-routes"
    probe = client.post(
        "/internal/custom-models/probe",
        json={"userId": user, "apiModel": "some-model", "providerId": "deepseek"},
    )
    assert probe.status_code == 400, probe.text
    assert "API Key" in probe.text

    discover = client.post(
        "/internal/custom-models/discover",
        json={"userId": user, "providerId": "deepseek"},
    )
    assert discover.status_code == 400, discover.text
    assert "API Key" in discover.text


def test_custom_entries_are_isolated_per_user(client: TestClient) -> None:
    owner = "test-custom-owner"
    stranger = "test-custom-stranger"
    provider = _create_provider(client, owner)

    catalog = client.get("/internal/models", params={"userId": stranger}).json()
    assert all(item["id"] != provider["id"] for item in catalog["providers"])


def test_context_window_out_of_range_is_rejected(client: TestClient) -> None:
    user = "test-custom-window"
    provider = _create_provider(client, user)

    for bad in (0, 1024, 20_000_000):
        response = _create_model(client, user, provider["id"], contextWindow=bad)
        assert response.status_code == 400, f"contextWindow={bad} should be rejected"


def test_delete_provider_cascades_models_and_key(client: TestClient) -> None:
    user = "test-custom-cascade"
    provider = _create_provider(client, user)
    model = _create_model(client, user, provider["id"]).json()

    response = client.delete(
        f"/internal/custom-providers/{provider['id']}", params={"userId": user}
    )
    assert response.status_code == 200, response.text

    catalog = client.get("/internal/models", params={"userId": user}).json()
    assert all(item["id"] != provider["id"] for item in catalog["providers"])
    assert all(item["id"] != model["id"] for item in catalog["models"])
    # 级联删 Key：可用模型里不再出现它（否则会留下一个永远调不通的模型）
    available = client.get("/internal/available-models", params={"userId": user}).json()
    assert model["id"] not in {item["id"] for item in available["models"]}


def test_builtin_entries_are_read_only(client: TestClient) -> None:
    user = "test-custom-builtin"

    delete_model = client.delete(
        f"/internal/custom-models/{_BUILTIN_MODEL_ID}", params={"userId": user}
    )
    assert delete_model.status_code == 400

    patch_model = client.patch(
        f"/internal/custom-models/{_BUILTIN_MODEL_ID}",
        json={"userId": user, "name": "hacked"},
    )
    assert patch_model.status_code == 400

    delete_provider = client.delete(
        "/internal/custom-providers/deepseek", params={"userId": user}
    )
    assert delete_provider.status_code == 400


def test_custom_model_requires_existing_provider(client: TestClient) -> None:
    response = _create_model(
        client, "test-custom-orphan", "custom.provider:deadbeef"
    )
    assert response.status_code == 400


def test_custom_model_can_extend_builtin_provider(client: TestClient) -> None:
    """内置供应商下加模型：端点来自代码常量，模型数要跟着变。"""
    user = "test-custom-builtin-provider"
    response = _create_model(client, user, "deepseek", apiModel="deepseek-v4.1-nitro")
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["providerId"] == "deepseek"

    catalog = client.get("/internal/models", params={"userId": user}).json()
    assert any(item["id"] == created["id"] for item in catalog["models"])
    deepseek = next(item for item in catalog["providers"] if item["id"] == "deepseek")
    # 内置 2 个 + 新增 1 个
    assert deepseek["modelCount"] == 3


# ── 3. 目录查找与默认模型回退 ──────────────────────────────────────────


class _ExplodingCustom:
    """任何访问都立即失败：用于证明内置模型查询不会触达自定义存储。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"custom storage must not be touched for built-in models: {name}")


def test_builtin_lookup_does_not_touch_custom_storage() -> None:
    catalog = Catalog(custom=_ExplodingCustom())  # type: ignore[arg-type]
    definition = asyncio.run(catalog.find_for("any-user", _BUILTIN_MODEL_ID))
    assert definition is not None
    assert definition.id == _BUILTIN_MODEL_ID


def test_unknown_custom_model_returns_none() -> None:
    catalog = Catalog()
    assert asyncio.run(catalog.find_for("any-user", "custom.model:missing")) is None


class _FakeSettings:
    def __init__(self, default_model_id: str) -> None:
        self._default = default_model_id

    async def load_default_model_id(self, user_id: str) -> str:
        return self._default

    async def load_personality(self, user_id: str) -> str:
        return ""


class _FakeProviders:
    async def load_api_key(self, user_id: str, provider_id: str) -> str:
        return "sk-fake-key"


def _load_configuration(default_model_id: str, model_id: str = "") -> object:
    configurations = RunConfigurations(
        models=Catalog(),
        settings=_FakeSettings(default_model_id),  # type: ignore[arg-type]
        providers=_FakeProviders(),  # type: ignore[arg-type]
    )
    request = Request(
        request_id="req-test",
        user_id="user-test",
        session_id="session-test",
        message="hi",
        model_id=model_id,
    )
    tracer = Tracer(None, Trace(request_id="req-test", user_id="user-test", session_id="session-test"))
    return asyncio.run(configurations.load(request, tracer))


def test_default_model_falls_back_when_it_was_deleted() -> None:
    """用户删掉自定义模型后，默认模型指向的 ID 已不存在 —— 必须回退而不是报错。"""
    configured = _load_configuration("custom.model:deleted")
    assert not isinstance(configured, Error), configured
    assert configured.definition.id == DEFAULT_MODEL_ID
    assert configured.request.model_id == DEFAULT_MODEL_ID


def test_explicit_model_is_not_silently_replaced() -> None:
    """显式指定的模型不存在时直接报错：静默换模型比报错更糟。"""
    configured = _load_configuration("", model_id="custom.model:deleted")
    assert isinstance(configured, Error)
    assert configured.type == "invalid_request"
