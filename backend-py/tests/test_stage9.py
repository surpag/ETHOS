"""阶段 9 验收测试：工作区（P0 Phase A）——实体、指令覆盖、自动记忆、级联删除。

对应 `docs/p0-phase-a-design.md` 与用户澄清（工作区 = 逻辑容器）：
- 工作区实体：POST/GET /internal/workspaces + DELETE /internal/workspaces/{id}（级联删指令）
- 工作区指令：GET/PUT/DELETE /internal/workspaces/{id}/instructions（整段覆盖，等价 AGENTS.md）
- 自动记忆：GET/PUT /internal/workspaces/{id}/memories + DELETE .../memories/{mid}
"""

from __future__ import annotations


def test_workspace_create_and_list(client) -> None:
    """创建工作区（系统生成 workspace_id）+ 列表。"""
    resp = client.post(
        "/internal/workspaces",
        json={"userId": "u-ws", "name": "我的 Python 后端项目"},
    )
    assert resp.status_code == 200
    workspace = resp.json()
    assert workspace["name"] == "我的 Python 后端项目"
    assert len(workspace["workspaceId"]) == 32  # uuid4().hex

    resp = client.get("/internal/workspaces", params={"userId": "u-ws"})
    assert resp.status_code == 200
    workspaces = resp.json()["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["name"] == "我的 Python 后端项目"


def test_workspace_duplicate_name_conflict(client) -> None:
    """同用户同名工作区 → 409；不同用户同名 → 允许。"""
    resp = client.post(
        "/internal/workspaces",
        json={"userId": "u-dup", "name": "同名工作区"},
    )
    assert resp.status_code == 200
    resp = client.post(
        "/internal/workspaces",
        json={"userId": "u-dup", "name": "同名工作区"},
    )
    assert resp.status_code == 409

    resp = client.post(
        "/internal/workspaces",
        json={"userId": "u-other", "name": "同名工作区"},
    )
    assert resp.status_code == 200


def test_instruction_save_load_overwrite(client) -> None:
    """工作区指令：整段保存 → 读取 → 覆盖（旧内容不残留）。"""
    resp = client.post(
        "/internal/workspaces",
        json={"userId": "u-instr", "name": "指令工作区"},
    )
    workspace_id = resp.json()["workspaceId"]

    resp = client.put(
        f"/internal/workspaces/{workspace_id}/instructions",
        json={"userId": "u-instr",
              "content": "技术栈：FastAPI\n规范：函数带 docstring"},
    )
    assert resp.status_code == 200

    resp = client.get(
        f"/internal/workspaces/{workspace_id}/instructions",
        params={"userId": "u-instr"},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "技术栈：FastAPI\n规范：函数带 docstring"

    # 覆盖保存：只留新内容
    client.put(
        f"/internal/workspaces/{workspace_id}/instructions",
        json={"userId": "u-instr", "content": "新指令"},
    )
    resp = client.get(
        f"/internal/workspaces/{workspace_id}/instructions",
        params={"userId": "u-instr"},
    )
    assert resp.json()["content"] == "新指令"


def test_instruction_isolation_between_workspaces(client) -> None:
    """不同工作区的指令互不串。"""
    a = client.post("/internal/workspaces", json={"userId": "u-iso", "name": "工作区A"}).json()["workspaceId"]
    b = client.post("/internal/workspaces", json={"userId": "u-iso", "name": "工作区B"}).json()["workspaceId"]
    client.put(f"/internal/workspaces/{a}/instructions",
               json={"userId": "u-iso", "content": "A 的指令"})

    resp_a = client.get(f"/internal/workspaces/{a}/instructions", params={"userId": "u-iso"})
    resp_b = client.get(f"/internal/workspaces/{b}/instructions", params={"userId": "u-iso"})
    assert resp_a.json()["content"] == "A 的指令"
    assert resp_b.json()["content"] == ""


def test_instruction_clear(client) -> None:
    """DELETE 清空指令。"""
    resp = client.post("/internal/workspaces", json={"userId": "u-clr", "name": "清空工作区"})
    workspace_id = resp.json()["workspaceId"]
    client.put(f"/internal/workspaces/{workspace_id}/instructions",
               json={"userId": "u-clr", "content": "要清空"})

    resp = client.delete(
        f"/internal/workspaces/{workspace_id}/instructions",
        params={"userId": "u-clr"},
    )
    assert resp.status_code == 204
    resp = client.get(f"/internal/workspaces/{workspace_id}/instructions", params={"userId": "u-clr"})
    assert resp.json()["content"] == ""


def test_workspace_delete_cascades(client) -> None:
    """删除工作区 → 级联删除指令 + 自动记忆。"""
    resp = client.post("/internal/workspaces", json={"userId": "u-cas", "name": "级联工作区"})
    workspace_id = resp.json()["workspaceId"]
    client.put(f"/internal/workspaces/{workspace_id}/instructions",
               json={"userId": "u-cas", "content": "级联删除我"})
    client.put(f"/internal/workspaces/{workspace_id}/memories",
               json={"userId": "u-cas", "content": "自动记忆也删"})

    resp = client.delete(f"/internal/workspaces/{workspace_id}", params={"userId": "u-cas"})
    assert resp.status_code == 204

    # 指令与自动记忆都应被级联清空
    resp = client.get(f"/internal/workspaces/{workspace_id}/instructions", params={"userId": "u-cas"})
    assert resp.json()["content"] == ""
    resp = client.get(f"/internal/workspaces/{workspace_id}/memories", params={"userId": "u-cas"})
    assert resp.json()["memories"] == []

    # 再次删除 → 404
    resp = client.delete(f"/internal/workspaces/{workspace_id}", params={"userId": "u-cas"})
    assert resp.status_code == 404


def test_workspace_delete_cross_user_protected(client) -> None:
    """跨用户删除他人工作区 → 404（不级联、不误删）。"""
    resp = client.post("/internal/workspaces", json={"userId": "u-owner", "name": "拥有者工作区"})
    workspace_id = resp.json()["workspaceId"]
    client.put(f"/internal/workspaces/{workspace_id}/instructions",
               json={"userId": "u-owner", "content": "owner 指令"})

    resp = client.delete(f"/internal/workspaces/{workspace_id}", params={"userId": "u-other"})
    assert resp.status_code == 404

    # owner 的指令仍在
    resp = client.get(f"/internal/workspaces/{workspace_id}/instructions", params={"userId": "u-owner"})
    assert resp.json()["content"] == "owner 指令"


def test_auto_memory_idempotent(client) -> None:
    """自动记忆：重复添加同一条 → 幂等（返回已有 id）。"""
    resp = client.post("/internal/workspaces", json={"userId": "u-mem", "name": "记忆工作区"})
    workspace_id = resp.json()["workspaceId"]
    r1 = client.put(f"/internal/workspaces/{workspace_id}/memories",
                    json={"userId": "u-mem", "content": "用户偏好简洁回答"})
    r2 = client.put(f"/internal/workspaces/{workspace_id}/memories",
                    json={"userId": "u-mem", "content": "用户偏好简洁回答"})
    assert r1.json()["id"] == r2.json()["id"]
    resp = client.get(f"/internal/workspaces/{workspace_id}/memories", params={"userId": "u-mem"})
    assert len(resp.json()["memories"]) == 1


def test_auto_memory_delete(client) -> None:
    """删除单条自动记忆。"""
    resp = client.post("/internal/workspaces", json={"userId": "u-md", "name": "记忆删除"})
    workspace_id = resp.json()["workspaceId"]
    r = client.put(f"/internal/workspaces/{workspace_id}/memories",
                   json={"userId": "u-md", "content": "待删除记忆"})
    memory_id = r.json()["id"]

    resp = client.delete(
        f"/internal/workspaces/{workspace_id}/memories/{memory_id}",
        params={"userId": "u-md"},
    )
    assert resp.status_code == 204
    resp = client.get(f"/internal/workspaces/{workspace_id}/memories", params={"userId": "u-md"})
    assert resp.json()["memories"] == []


def test_workspace_validation(client) -> None:
    """缺少 userId / name / content 时返回 4xx。"""
    # 缺 userId
    resp = client.get("/internal/workspaces")
    assert resp.status_code == 400

    # 缺 name
    resp = client.post("/internal/workspaces", json={"userId": "u-val"})
    assert resp.status_code == 400

    # 空 name
    resp = client.post("/internal/workspaces", json={"userId": "u-val", "name": ""})
    assert resp.status_code == 400

    # 自动记忆缺 content
    resp = client.post("/internal/workspaces", json={"userId": "u-val2", "name": "校验工作区"})
    workspace_id = resp.json()["workspaceId"]
    resp = client.put(f"/internal/workspaces/{workspace_id}/memories", json={"userId": "u-val2", "content": ""})
    assert resp.status_code == 400


def test_injectable_format(client) -> None:
    """load_injectable 输出：指令优先 + 自动记忆补充。"""
    import asyncio

    import aiosqlite

    from edith_backend.internal.workspace.memory_store import WorkspaceMemoryStore
    from edith_backend.internal.workspace.schema import create_schema

    async def scenario() -> str:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        store = WorkspaceMemoryStore(db)
        await store.save_instruction("u-inj", "ws-inj", "技术栈：FastAPI")
        await store.add("u-inj", "ws-inj", "偏好简洁")
        text = await store.load_injectable("u-inj", "ws-inj")
        await db.close()
        return text

    text = asyncio.run(scenario())
    assert text == "技术栈：FastAPI\n\n【自动记忆】\n- 偏好简洁"
