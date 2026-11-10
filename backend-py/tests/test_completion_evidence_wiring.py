"""完成证据的透传测试：事件 / 落库 / 历史投影三者必须同源同值。

上一版只把证据写进 trace，用户界面完全看不到 —— 那等于「系统知道，但用户不知道」。
这里锁住三条链路：

1. 终态事件携带 `completionEvidence`（SSE，camelCase，None 时整字段省略）；
2. `chat_messages.completion_evidence` 落库，历史读取回来一致；
3. 历史投影把证据挂到 AssistantBlock 上（刷新后仍能看到）。

以及一条很容易被写错的不变式：**旧行是 NULL（未知），不能当成 "none"（无工具调用）**。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from edith_backend.internal.agentrun.execution import (
    EVIDENCE_MUTATED,
    EVIDENCE_NONE,
)
from edith_backend.internal.agentstream.types import Event
from edith_backend.internal.conversation.history import _timeline
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore


async def _fresh_store() -> tuple[aiosqlite.Connection, SessionStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, SessionStore(db)


def test_event_serializes_camel_case_evidence() -> None:
    """SSE 走 model_dump_json(exclude_none=True)：有值时字段名必须是 completionEvidence。"""
    raw = Event(
        type="run.completed",
        request_id="r1",
        session_id="s1",
        completion_evidence=EVIDENCE_MUTATED,
    ).model_dump_json(exclude_none=True, by_alias=True)
    payload = json.loads(raw)
    assert payload["completionEvidence"] == EVIDENCE_MUTATED


def test_event_omits_evidence_when_unknown() -> None:
    """未知时整字段省略（而不是输出 null）——前端必须用 typeof 判空，不能用 === null。"""
    payload = json.loads(
        Event(type="run.completed", request_id="r1").model_dump_json(
            exclude_none=True, by_alias=True
        )
    )
    assert "completionEvidence" not in payload


def test_evidence_round_trips_through_store_and_history() -> None:
    """落库 → get_records → Timeline 投影，证据必须原样到达前端。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.add_assistant_message(
                "u1", "s1", "已为您创建文件", "", None, [], 1200, EVIDENCE_MUTATED
            )
            records = await store.get_records("u1", "s1")
            assert records[0].completion_evidence == EVIDENCE_MUTATED

            timeline = _timeline(records)
            assert timeline.blocks[0].completion_evidence == EVIDENCE_MUTATED
            assert timeline.blocks[0].duration_ms == 1200
        finally:
            await db.close()

    asyncio.run(go())


def test_legacy_row_without_evidence_is_unknown_not_none() -> None:
    """该列引入前的历史消息是 NULL：显示为「未知」，不能凭空指控它没调用工具。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            # 绕过 add_assistant_message 的默认值，模拟迁移前的旧行
            await store._db.execute(
                "INSERT INTO chat_messages (user_id, session_id, role, content)"
                " VALUES ('u1', 's1', 'assistant', '旧消息')"
            )
            await store._db.commit()
            records = await store.get_records("u1", "s1")
            assert records[0].completion_evidence is None
            assert _timeline(records).blocks[0].completion_evidence is None
        finally:
            await db.close()

    asyncio.run(go())


@pytest.mark.parametrize("evidence", [EVIDENCE_MUTATED, EVIDENCE_NONE])
def test_user_message_has_no_evidence(evidence: str) -> None:
    """用户消息不参与证据判定：只校验 assistant 行写入时该列不被污染。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.add_user_message("u1", "s1", "帮我写个文件")
            await store.add_assistant_message(
                "u1", "s1", "好的", "", None, [], 10, evidence
            )
            records = await store.get_records("u1", "s1")
            assert records[0].role == "user"
            assert records[0].completion_evidence is None
            assert records[1].completion_evidence == evidence
        finally:
            await db.close()

    asyncio.run(go())
