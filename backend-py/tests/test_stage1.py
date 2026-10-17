"""阶段 1 验收测试：配置 + 数据库 + 模型目录 + 用户设置。

用临时数据库隔离，验证 models 与 userconfig 的 HTTP 契约与 Go 版一致。
client fixture 由 conftest.py 提供（session 级，共享单次 lifespan）。
各测试通过独立 userId 隔离数据。
"""

from __future__ import annotations


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_catalog(client) -> None:
    response = client.get("/internal/models")
    assert response.status_code == 200
    data = response.json()
    assert len(data["providers"]) == 6
    assert len(data["models"]) == 15
    # 抽查一个模型的 camelCase 字段与默认模型
    mini = next(m for m in data["models"] if m["id"] == "minimax.m3")
    assert mini["providerId"] == "minimax"
    assert mini["capabilities"] == {"vision": True}
    assert mini["reasoningOptions"] == []


def test_available_models_empty_without_keys(client) -> None:
    response = client.get("/internal/available-models?userId=u-empty")
    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_user_settings_roundtrip(client) -> None:
    # 新用户读取：默认模型兜底为 minimax.m3
    response = client.get("/internal/user-settings?userId=u-roundtrip")
    assert response.status_code == 200
    assert response.json()["defaultModelId"] == "minimax.m3"

    # 保存设置与密钥
    response = client.put(
        "/internal/user-settings",
        json={
            "userId": "u-roundtrip",
            "personality": "你是测试助手",
            "defaultModelId": "minimax.m3",
            "timezone": "Asia/Shanghai",
            "providers": [
                {"providerId": "deepseek", "apiKey": "sk-d"},
                {"providerId": "minimax", "apiKey": "sk-m"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["personality"] == "你是测试助手"
    assert body["timezone"] == "Asia/Shanghai"
    assert {p["providerId"]: p["hasApiKey"] for p in body["providers"]} == {
        "deepseek": True,
        "minimax": True,
    }

    # 配置密钥后，可用模型应包含 deepseek 与 minimax 的模型
    available = client.get("/internal/available-models?userId=u-roundtrip").json()
    provider_ids = {m["providerId"] for m in available["models"]}
    assert provider_ids == {"deepseek", "minimax"}


def test_empty_api_key_does_not_overwrite(client) -> None:
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-overwrite",
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "deepseek", "apiKey": "sk-original"}],
        },
    )
    # 空 apiKey 不应覆盖已保存密钥
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-overwrite",
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "deepseek", "apiKey": ""}],
        },
    )
    response = client.get("/internal/user-settings?userId=u-overwrite")
    statuses = {p["providerId"]: p["hasApiKey"] for p in response.json()["providers"]}
    assert statuses["deepseek"] is True


def test_missing_user_id_returns_400(client) -> None:
    response = client.get("/internal/user-settings")
    assert response.status_code == 400
    assert response.json() == {
        "type": "invalid_request",
        "message": "userId is required",
    }


def test_unknown_field_returns_400(client) -> None:
    response = client.put(
        "/internal/user-settings",
        json={"userId": "u-unknown", "defaultModelId": "minimax.m3", "bogusField": 1},
    )
    assert response.status_code == 400
    assert response.json()["type"] == "invalid_request"


def test_missing_default_model_id_returns_400(client) -> None:
    response = client.put("/internal/user-settings", json={"userId": "u-missing"})
    assert response.status_code == 400
    assert response.json() == {
        "type": "invalid_request",
        "message": "userId and defaultModelId are required",
    }
