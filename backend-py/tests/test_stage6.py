"""阶段 6 验收测试：systemtools、cronjob CRUD、MCP 配置与安全校验。"""

from __future__ import annotations

import json
from datetime import UTC

from edith_backend.internal.systemtools import SystemToolSet
from edith_backend.internal.userconfig.mcp import (
    InvalidMCPServer,
    MCPServerInput,
    _validate_input,
)


def test_get_current_time() -> None:
    toolset = SystemToolSet()
    definitions = toolset.tool_definitions()
    assert definitions[0]["function"]["name"] == "get_current_time"


def test_get_current_time_execute() -> None:
    import asyncio
    toolset = SystemToolSet()
    result = asyncio.run(toolset.execute("get_current_time", {"timezone": "Asia/Shanghai"}, "u", "s"))
    data = json.loads(result)
    assert "time" in data
    assert data["timezone"] == "Asia/Shanghai"


def test_get_current_time_invalid_timezone() -> None:
    import asyncio
    toolset = SystemToolSet()
    result = asyncio.run(toolset.execute("get_current_time", {"timezone": "Bad/Zone"}, "u", "s"))
    assert "error" in json.loads(result)


def test_cronjob_crud(client) -> None:
    # 创建一次性任务
    resp = client.post(
        "/internal/cron-jobs",
        json={"userId": "u-cron", "name": "测试任务", "taskType": "once",
              "schedule": "2026-12-31T10:00:00+08:00", "prompt": "执行测试"},
    )
    assert resp.status_code == 200
    job = resp.json()
    assert job["taskType"] == "once"
    assert job["enabled"] is True

    # 列表
    listing = client.get("/internal/cron-jobs?userId=u-cron").json()
    assert any(j["id"] == job["id"] for j in listing["jobs"])

    # 删除
    resp = client.delete(f"/internal/cron-jobs/{job['id']}?userId=u-cron")
    assert resp.status_code == 204


def test_cronjob_invalid_schedule_returns_400(client) -> None:
    resp = client.post(
        "/internal/cron-jobs",
        json={"userId": "u-cron", "name": "bad", "taskType": "recurring",
              "schedule": "not-a-cron", "prompt": "x"},
    )
    assert resp.status_code == 400


def test_cronjob_recurring_schedule() -> None:
    # 直接测试 schedule_rules 的 cron 解析
    import asyncio
    from datetime import datetime

    from edith_backend.internal.cronjob.schedule_rules import next_run

    class _FakeSettings:
        async def load_timezone(self, user_id):
            return "Asia/Shanghai"

    result = asyncio.run(
        next_run(_FakeSettings(), "u", 
                 type("I", (), {"name": "n", "task_type": "recurring", "schedule": "0 9 * * *", "prompt": "p"})(),
                 datetime(2026, 8, 13, 0, 0, tzinfo=UTC))
    )
    assert result is not None


def test_mcp_rejects_localhost() -> None:
    try:
        _validate_input(MCPServerInput(name="x", url="http://localhost:8080", transport="streamable"), True)
        assert False, "should reject localhost"
    except InvalidMCPServer:
        pass


def test_mcp_rejects_private_ip() -> None:
    try:
        _validate_input(MCPServerInput(name="x", url="http://127.0.0.1:8080", transport="streamable"), True)
        assert False, "should reject loopback"
    except InvalidMCPServer:
        pass


def test_mcp_accepts_public_url() -> None:
    result = _validate_input(
        MCPServerInput(name="x", url="https://api.example.com/mcp", transport="streamable"), True
    )
    assert result.name == "x"


def test_mcp_crud(client) -> None:
    resp = client.post(
        "/internal/mcp-servers",
        json={"userId": "u-mcp", "name": "搜索", "url": "https://mcp.example.com",
              "transport": "streamable", "enabled": True,
              "headers": [{"name": "Authorization", "value": "Bearer x"}]},
    )
    assert resp.status_code == 200
    server = resp.json()
    assert server["transport"] == "streamable"
    # header 只返回 hasValue，不返回真实值
    assert server["headers"][0]["hasValue"] is True
    assert "value" not in server["headers"][0]

    listing = client.get("/internal/mcp-servers?userId=u-mcp").json()
    assert any(s["id"] == server["id"] for s in listing["servers"])
