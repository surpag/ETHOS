"""Run 终态语义（outcome）的透传测试。

背景：`end_reason` 只存在于一次 SSE 事件里，不落库 → 刷新后「任务未完成」标记消失，
同一条消息是否显示「未完成」取决于有没有刷新。这里锁住语义位的三条链路，
和 completion_evidence 是同一条纪律：

1. 终态事件携带 `outcome`（camelCase，None 时整字段省略）；
2. `chat_messages.end_reason` 落库，历史读取回来一致；
3. 历史投影把 `outcome` 挂到 AssistantBlock 上（刷新后仍能看到）。

以及两条不变式：

- **未知（NULL）不等于 incomplete**：把旧行判成「没做完」是凭空指控；
- **未知原因默认 incomplete**：后端新增任何一种终止原因，不需要改前端。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite

from edith_backend.internal.agentstream.outcome import (
    OUTCOME_COMPLETE,
    OUTCOME_INCOMPLETE,
    derive_outcome,
)
from edith_backend.internal.agentstream.types import Event
from edith_backend.internal.conversation.history import _timeline
from edith_backend.internal.conversation.types import ConversationResponse
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.usage.types import Summary


async def _fresh_store() -> tuple[aiosqlite.Connection, SessionStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, SessionStore(db)


def test_derive_outcome_three_states() -> None:
    """三态：未知 → None，completed → complete，其余一律 incomplete。"""
    assert derive_outcome(None) is None
    assert derive_outcome("completed") == OUTCOME_COMPLETE
    for reason in (
        "rounds_exhausted",
        "context_budget_exhausted",
        "output_truncated",
        "content_filtered",
        # 关键：将来新增的原因没有被枚举过，也必须落在 incomplete。
        # 这条断言就是「前端不需要跟着后端改」的保证。
        "some_future_reason",
    ):
        assert derive_outcome(reason) == OUTCOME_INCOMPLETE


def test_event_serializes_outcome_and_omits_when_unknown() -> None:
    payload = json.loads(
        Event(
            type="run.completed",
            request_id="r1",
            end_reason="rounds_exhausted",
            outcome=OUTCOME_INCOMPLETE,
        ).model_dump_json(exclude_none=True, by_alias=True)
    )
    assert payload["outcome"] == OUTCOME_INCOMPLETE
    assert payload["endReason"] == "rounds_exhausted"

    unknown = json.loads(
        Event(type="run.completed", request_id="r1").model_dump_json(
            exclude_none=True, by_alias=True
        )
    )
    assert "outcome" not in unknown


def test_outcome_round_trips_through_store_and_history() -> None:
    """落库 → get_records → Timeline 投影，语义位必须原样到达前端。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.add_assistant_message(
                "u1", "s1", "已完成一半", "", None, [], 900, "mutated",
                "rounds_exhausted",
            )
            records = await store.get_records("u1", "s1")
            assert records[0].end_reason == "rounds_exhausted"

            timeline = _timeline(records)
            block = timeline.blocks[0]
            assert block.type == "assistant"
            assert block.outcome == OUTCOME_INCOMPLETE
            assert block.end_reason == "rounds_exhausted"
        finally:
            await db.close()

    asyncio.run(go())


def test_legacy_row_outcome_stays_unknown() -> None:
    """该列引入前的旧行：outcome 必须是 None（未知），不能判成 incomplete。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.add_assistant_message("u1", "s1", "旧消息")
            block = _timeline(await store.get_records("u1", "s1")).blocks[0]
            assert block.outcome is None
            assert block.end_reason is None
        finally:
            await db.close()

    asyncio.run(go())


def test_conversation_response_exposes_task_status() -> None:
    """会话响应带 taskStatus：这是「还有没跑完的活」的权威来源，刷新后仍在。"""
    response = ConversationResponse(
        timeline={"blocks": []},
        usage=Summary(total_tokens=0, completion_tokens=0),
        task_status="in_progress",
    )
    payload = json.loads(response.model_dump_json(by_alias=True))
    assert payload["taskStatus"] == "in_progress"
