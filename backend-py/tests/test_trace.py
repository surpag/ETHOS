"""链路追踪（trace）单元测试。

覆盖：
1. 步骤落库 + 父子关联（ContextVar）+ 墙钟/自耗时/未打点分解
2. 异常被记录后继续抛出；业务错误（非异常）也能落库
3. 慢步骤告警与「未打点」提示
4. 幂等：同一 request_id 重复提交时整体覆盖，不混两次尝试的步骤
5. 保留期清理：一般 run 早删、失败/降级 run 晚删
6. store 为 None 时全部空转
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import aiosqlite
import pytest

from edith_backend.internal.trace import tracer as tracer_mod
from edith_backend.internal.trace.schema import create_schema
from edith_backend.internal.trace.store import Store
from edith_backend.internal.trace.tracer import new_tracer
from edith_backend.internal.trace.types import (
    DEGRADE_MODEL,
    ERROR,
    LLM_CALL,
    OK,
    PREPARE,
    TOOL_CALL,
    Trace,
)


async def _fresh_store() -> tuple[aiosqlite.Connection, Store]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, Store(db)


def test_steps_persisted_with_parent_linkage_and_timing() -> None:
    async def go() -> None:
        db, store = await _fresh_store()
        tracer = new_tracer(store, "req-1", "u1", "s1", user_input="帮我看看这个项目")
        await tracer.begin()

        async with tracer.step(PREPARE, "load_configuration") as outer:
            outer.metadata["model"] = "m1"
            async with tracer.step(LLM_CALL, "create#1") as inner:
                inner.metadata["ttft_ms"] = 42
                await asyncio.sleep(0.02)

        await tracer.finish(status=OK, end_reason="completed", duration_ms=100)

        steps = await store.get_steps("req-1")
        assert [s["name"] for s in steps] == ["load_configuration", "create#1"]
        outer_row, inner_row = steps
        assert outer_row["parent_seq"] is None and outer_row["depth"] == 0
        assert inner_row["parent_seq"] == outer_row["seq"]
        assert inner_row["depth"] == 1
        # seq 在「进入」时分配，父一定小于子（查看器靠它排序）
        assert outer_row["seq"] < inner_row["seq"]
        assert json.loads(inner_row["metadata"])["ttft_ms"] == 42

        trace = await store.get_trace("req-1")
        assert trace["status"] == OK
        assert trace["finished_at"] is not None
        # 自耗时只算顶层步骤：父的 duration 已包含子的，不能重复计入
        assert trace["self_time_ms"] == round(outer_row["duration_ms"])
        assert trace["self_time_ms"] < trace["duration_ms"]
        # 输入默认只存摘要（不落原文）
        assert trace["user_input"] == "帮我看看这个项目"
        await db.close()

    asyncio.run(go())


def test_step_error_is_recorded_and_still_raised() -> None:
    async def go() -> None:
        db, store = await _fresh_store()
        tracer = new_tracer(store, "req-2", "u1", "s1")
        await tracer.begin()

        with pytest.raises(ValueError, match="boom"):
            async with tracer.step(TOOL_CALL, "sandbox_run"):
                raise ValueError("boom")

        await tracer.finish(status=ERROR, end_reason="internal_error", duration_ms=5)

        steps = await store.get_steps("req-2")
        assert steps[0]["status"] == ERROR
        assert steps[0]["error_type"] == "ValueError"
        assert steps[0]["error_message"] == "boom"
        await db.close()

    asyncio.run(go())


def test_finish_records_business_error_without_exception() -> None:
    """早退分支（session_busy 等）用 error_type/error_message 直接落库。"""

    async def go() -> None:
        db, store = await _fresh_store()
        tracer = new_tracer(store, "req-3", "u1", "s1")
        await tracer.begin()
        await tracer.finish(
            status=ERROR,
            end_reason="session_busy",
            error_type="session_busy",
            error_message="an agent run is already active for this session",
        )
        trace = await store.get_trace("req-3")
        assert trace["status"] == ERROR
        assert trace["end_reason"] == "session_busy"
        assert trace["error_message"].startswith("an agent run")
        await db.close()

    asyncio.run(go())


def test_degrade_is_explicit() -> None:
    async def go() -> None:
        db, store = await _fresh_store()
        tracer = new_tracer(store, "req-4", "u1", "s1")
        await tracer.begin()
        tracer.degrade(DEGRADE_MODEL, "qwen-max", "deepseek-v4", "429")
        await tracer.finish(status=OK, duration_ms=10)
        trace = await store.get_trace("req-4")
        assert trace["degraded"] == 1
        assert json.loads(trace["degradations"]) == [
            {
                "kind": "model_fallback",
                "from": "qwen-max",
                "to": "deepseek-v4",
                "reason": "429",
            }
        ]
        await db.close()

    asyncio.run(go())


def test_slow_step_and_unattributed_warnings(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracer_mod, "SLOW_STEP_MS", 1.0)

    async def go() -> None:
        db, store = await _fresh_store()
        tracer = new_tracer(store, "req-5", "u1", "s1")
        await tracer.begin()
        async with tracer.step(LLM_CALL, "create#1"):
            await asyncio.sleep(0.02)
        # 墙钟远大于自耗时 → 触发「未打点」提示
        await tracer.finish(status=OK, duration_ms=5000)
        await db.close()

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "trace_slow" in messages
    assert "trace_unattributed" in messages


def test_same_request_id_replaces_previous_attempt() -> None:
    async def go() -> None:
        db, store = await _fresh_store()
        first = new_tracer(store, "req-6", "u1", "s1")
        await first.begin()
        async with first.step(LLM_CALL, "create#1"):
            pass
        await first.finish(status=ERROR, end_reason="internal_error")

        second = new_tracer(store, "req-6", "u1", "s1")
        await second.begin()
        async with second.step(LLM_CALL, "create#1"):
            pass
        await second.finish(status=OK, end_reason="completed")

        cursor = await db.execute(
            "SELECT COUNT(*) AS c FROM traces WHERE request_id = ?", ("req-6",)
        )
        assert (await cursor.fetchone())["c"] == 1
        await cursor.close()
        # 旧尝试的步骤必须被清掉，否则两棵树会混在一起
        steps = await store.get_steps("req-6")
        assert len(steps) == 1
        assert (await store.get_trace("req-6"))["status"] == OK
        await db.close()

    asyncio.run(go())


def test_cleanup_keeps_recent_and_error_traces() -> None:
    async def go() -> None:
        db, store = await _fresh_store()
        now = datetime.now().astimezone()
        # 20 天前：已过 14 天保留期，但未过失败/降级 run 的 28 天 → 只有一般 run 该删
        old = (now - timedelta(days=20)).isoformat(timespec="seconds")
        # 10 天前：都在保留期内
        recent = (now - timedelta(days=10)).isoformat(timespec="seconds")
        rows = [
            ("old-ok", OK, 0, old),
            ("old-error", ERROR, 0, old),
            ("old-degraded", OK, 1, old),
            ("recent-ok", OK, 0, recent),
        ]
        for request_id, status, degraded, started in rows:
            await store.insert_trace(
                Trace(
                    request_id=request_id,
                    user_id="u1",
                    session_id="s1",
                    started_at=started,
                    status=status,
                    degraded=bool(degraded),
                )
            )

        removed = await store.cleanup(days=14)
        assert removed == 1
        remaining = {t["request_id"] for t in await store.list_traces("u1", limit=10)}
        assert remaining == {"old-error", "old-degraded", "recent-ok"}
        await db.close()

    asyncio.run(go())


def test_disabled_tracer_is_noop() -> None:
    tracer = new_tracer(None, "req-7", "u1", "s1")
    assert tracer.enabled is False

    async def go() -> None:
        await tracer.begin()
        async with tracer.step(LLM_CALL, "create#1") as s:
            s.output = "x"
        await tracer.flush()
        await tracer.finish(status=OK, duration_ms=1)

    asyncio.run(go())
    # 完全空转：不落盘、也不在内存里留痕（store=None 即总开关关闭）
    assert tracer.trace.steps == []
    assert tracer.trace.status == "running"
