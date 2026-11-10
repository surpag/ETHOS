"""运行中进度落库：刷新页面不该丢掉已经发生的执行过程。

这些断言守的是**写序**与**幂等**，不是「能写进去」：
1. 一次 run 只有一条助手消息（进度写 + 终态写落在同一行）；
2. 进度写不能把已经写好的结论（耗时/证据/终止原因）擦回 NULL；
3. 进度落库后，历史投影里当场就能看到工具调用与推理，且块 id 与 SSE 同源
   —— 否则刷新后「历史那条」和「重连补发那条」会变成两份回复。
"""

from __future__ import annotations

import asyncio

import aiosqlite

from edith_backend.internal.agentrun.progress import ProgressRecorder
from edith_backend.internal.agentstream.types import Event
from edith_backend.internal.conversation.history import History
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.usage.types import Summary


async def _store() -> tuple[SessionStore, aiosqlite.Connection]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    store = SessionStore(db)
    await create_schema(db)
    return store, db


class _StubUsage:
    """用量汇总与本次断言无关：只要是个 Summary，不要真的去查表。"""

    async def session_summary(self, user_id: str, session_id: str) -> Summary:
        return Summary(total_tokens=0, completion_tokens=0)


def _event(kind: str, **fields) -> Event:
    return Event(type=kind, **fields)


def test_progress_and_final_write_share_one_row() -> None:
    """进度写与终态写必须是同一行：否则刷新后一步回复会变成两步。"""

    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.add_user_message("u1", "s1", "帮我看下目录")
            recorder = ProgressRecorder(
                store, user_id="u1", session_id="s1", request_id="r1"
            )
            recorder.observe(_event("message.delta", delta="先看看"))
            recorder.observe(
                _event(
                    "tool.started",
                    tool_call_id="t1",
                    tool_name="list_files",
                    arguments="{}",
                    tool_status="running",
                )
            )
            recorder.observe(
                _event(
                    "tool.finished",
                    tool_call_id="t1",
                    tool_status="completed",
                    tool_result="a.txt",
                )
            )
            await recorder.stop()

            # 终态：同一 request_id 再写一次，带结论字段
            await store.add_assistant_message(
                "u1",
                "s1",
                "先看看a.txt",
                "",
                None,
                [{"id": "t1", "name": "list_files", "arguments": "{}",
                  "status": "completed", "result": "a.txt"}],
                1234,
                "mutated",
                "completed",
                "r1",
            )

            records = await store.get_records("u1", "s1")
            assistants = [r for r in records if r.role == "assistant"]
            assert len(assistants) == 1
            assert assistants[0].content == "先看看a.txt"
            assert assistants[0].duration_ms == 1234
            assert assistants[0].end_reason == "completed"
            assert assistants[0].request_id == "r1"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_progress_write_does_not_erase_conclusion_fields() -> None:
    """迟到的进度写只该更新内容，不能把结论擦回 NULL。

    顺序保证由 `stop()` 负责；这里是第二道防线：即使调用方写反了，结论也不会丢
    ——否则「已完成并产生变更」会在刷新后变成「没有结论」。
    """

    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.add_assistant_message(
                "u1", "s1", "做完了", "", None, [], 900, "mutated", "completed", "r1"
            )
            # 后到的进度写：内容更旧、结论字段全 NULL
            await store.add_assistant_message(
                "u1", "s1", "做完了", "", None, [], None, None, None, "r1"
            )
            records = await store.get_records("u1", "s1")
            assert len(records) == 1
            assert records[0].duration_ms == 900
            assert records[0].completion_evidence == "mutated"
            assert records[0].end_reason == "completed"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_history_exposes_in_flight_progress_with_sse_compatible_id() -> None:
    """run 还没结束，历史里就该有内容；且 id 与 SSE 的 assistantId 一致。"""

    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.add_user_message("u1", "s1", "帮我看看目录")
            recorder = ProgressRecorder(
                store, user_id="u1", session_id="s1", request_id="r1"
            )
            recorder.observe(_event("reasoning.delta", delta="先确认目录"))
            recorder.observe(_event("message.delta", delta="目录里有"))
            recorder.observe(
                _event(
                    "tool.started",
                    tool_call_id="t1",
                    tool_name="list_files",
                    arguments="{}",
                    tool_status="running",
                )
            )
            recorder.observe(
                _event(
                    "tool.finished",
                    tool_call_id="t1",
                    tool_status="completed",
                    tool_result="a.txt",
                )
            )
            await recorder.stop()

            history = History(store, _StubUsage())
            conversation = await history.get("u1", "s1")
            assistants = [
                block for block in conversation.timeline.blocks
                if block.type == "assistant"
            ]
            assert len(assistants) == 1
            block = assistants[0]
            # service 侧生成的是 assistant_{request_id}；两处必须对得上，
            # 否则重连补发会再建一个块，屏幕上出现两份回复。
            assert block.id == "assistant_r1"
            assert block.request_id == "r1"
            kinds = [item.type for item in block.blocks]
            assert kinds == ["reasoning", "tool", "text"]
            assert block.blocks[1].tool_name == "list_files"
            assert block.blocks[1].result == "a.txt"
            # 没有结论 = 不知道，不能当作「已完成」
            assert block.outcome is None
            assert block.duration_ms is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recorder_is_silent_when_nothing_happened() -> None:
    """一条事件都没有时不写空行：空白助手块比没有更让人困惑。"""

    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.add_user_message("u1", "s1", "你好")
            recorder = ProgressRecorder(
                store, user_id="u1", session_id="s1", request_id="r1"
            )
            recorder.observe(_event("run.started", assistant_id="assistant_r1"))
            await recorder.stop()
            records = await store.get_records("u1", "s1")
            assert [r.role for r in records] == ["user"]
        finally:
            await db.close()

    asyncio.run(scenario())
