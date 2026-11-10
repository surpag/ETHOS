"""plan_update 工具的行为测试：拒绝要说清原因，成功要落库并广播。

工具层的职责边界很窄：校验、落库、发事件。它**不判断任务是否完成**——
那是 verify.completion 的事；它只保证「计划里打的每一个勾都有出处」。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite

from edith_backend.internal.agentrun.plan_tools import PlanToolSet
from edith_backend.internal.agentrun.run_plan import STATUS_DONE, STATUS_IN_PROGRESS
from edith_backend.internal.session.plan_store import RunPlanStore
from edith_backend.internal.session.schema import create_schema


def _fact(call_id: str = "c1", name: str = "sandbox_write_file") -> dict:
    return {
        "id": call_id,
        "name": name,
        "arguments": '{"path":"workspace/w/src/a.py"}',
        "status": "completed",
        "result": '{"ok":true}',
    }


async def _setup():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    store = RunPlanStore(db)
    return db, store


def test_tool_is_registered_with_strict_schema() -> None:
    from edith_backend.internal.agentrun.run_plan import RunPlanRegistry

    tool_set = PlanToolSet(RunPlanRegistry(), store=None)  # type: ignore[arg-type]
    definitions = tool_set.tool_definitions()
    assert [d["function"]["name"] for d in definitions] == ["plan_update"]
    params = definitions[0]["function"]["parameters"]
    assert params["required"] == ["mode", "items"]
    # status 必须是枚举，不接受自由文本（否则模型会发明状态语义）
    item_schema = params["properties"]["items"]["items"]
    assert set(item_schema["properties"]["status"]["enum"]) == {
        "pending",
        "in_progress",
        "done",
        "blocked",
    }


def test_execute_without_active_run_is_rejected() -> None:
    async def go() -> None:
        db, store = await _setup()
        try:
            from edith_backend.internal.agentrun.run_plan import RunPlanRegistry

            tool_set = PlanToolSet(RunPlanRegistry(), store)
            result = json.loads(
                await tool_set.execute(
                    "plan_update",
                    {"mode": "set", "items": [{"id": "1", "title": "x"}]},
                    "u1",
                    "s1",
                )
            )
            assert result["kind"] == "command_error"
            assert "没有正在执行的任务" in result["error"]
        finally:
            await db.close()

    asyncio.run(go())


def test_success_persists_and_emits_snapshot() -> None:
    async def go() -> None:
        db, store = await _setup()
        try:
            from edith_backend.internal.agentrun.run_plan import RunPlanRegistry

            registry = RunPlanRegistry()
            facts = [_fact()]
            events: list = []
            registry.open(
                user_id="u1",
                session_id="s1",
                request_id="r1",
                facts=facts,
                emit=events.append,
            )
            tool_set = PlanToolSet(registry, store)
            result = json.loads(
                await tool_set.execute(
                    "plan_update",
                    {
                        "mode": "set",
                        "items": [
                            {
                                "id": "1",
                                "title": "写 a.py",
                                "status": STATUS_DONE,
                                "evidence": {
                                    "tool_call_ids": ["c1"],
                                    "path": "src/a.py",
                                },
                            },
                            {"id": "2", "title": "跑测试", "status": STATUS_IN_PROGRESS},
                        ],
                    },
                    "u1",
                    "s1",
                )
            )
            assert result["ok"] is True
            assert result["progress"]["done"] == 1
            assert result["items"][0]["verified"] is True

            # 事件：一份完整快照 + revision，前端据此幂等覆盖
            assert len(events) == 1
            event = events[0]
            assert event.type == "plan.updated"
            assert event.plan.revision == 1
            assert [item.id for item in event.plan.items] == ["1", "2"]
            assert event.plan.items[0].verified is True
            assert event.plan.items[1].verified is None

            # 落库：刷新后能回填同一份
            latest = await store.latest("u1", "s1")
            assert latest is not None and latest["revision"] == 1
            assert latest["items"][0]["status"] == STATUS_DONE
        finally:
            await db.close()

    asyncio.run(go())


def test_rejection_is_actionable_and_does_not_emit() -> None:
    async def go() -> None:
        db, store = await _setup()
        try:
            from edith_backend.internal.agentrun.run_plan import RunPlanRegistry

            registry = RunPlanRegistry()
            events: list = []
            registry.open(
                user_id="u1",
                session_id="s1",
                request_id="r1",
                facts=[],  # 没有任何工具调用
                emit=events.append,
            )
            tool_set = PlanToolSet(registry, store)
            result = json.loads(
                await tool_set.execute(
                    "plan_update",
                    {
                        "mode": "set",
                        "items": [
                            {
                                "id": "1",
                                "title": "写 a.py",
                                "status": STATUS_DONE,
                                "evidence": {"path": "src/a.py"},
                            }
                        ],
                    },
                    "u1",
                    "s1",
                )
            )
            assert result["kind"] == "verification_failed"
            assert result["retryable"] is True  # 业务性拒绝：可修正后重试，不触发熔断
            assert any("还没有任何已完成的工具调用" in failure for failure in result["failures"])
            assert "suggestion" in result
            # 被拒绝的更新不广播、不落库
            assert events == []
            assert await store.latest("u1", "s1") is None
        finally:
            await db.close()

    asyncio.run(go())


def test_unknown_tool_name_is_rejected() -> None:
    async def go() -> None:
        db, store = await _setup()
        try:
            from edith_backend.internal.agentrun.run_plan import RunPlanRegistry

            tool_set = PlanToolSet(RunPlanRegistry(), store)
            result = json.loads(
                await tool_set.execute("plan_delete", {}, "u1", "s1")
            )
            assert "unknown plan tool" in result["error"]
        finally:
            await db.close()

    asyncio.run(go())
