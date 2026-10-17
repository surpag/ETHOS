"""完成时长（duration_ms）的持久化与投影测试。

覆盖三块：
1. `elapsed_ms` 计时助手
2. 耗时随 assistant 消息落库，并能被历史 Timeline 投影出来
3. 旧库迁移：`chat_messages` 缺 duration_ms 列时由 `_migrate` 幂等补齐
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite

from edith_backend.internal.agentrun.execution import elapsed_ms
from edith_backend.internal.conversation.history import _timeline
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore


def test_elapsed_ms_measures_monotonic_delta() -> None:
    start = time.monotonic()
    time.sleep(0.05)
    ms = elapsed_ms(start)
    assert isinstance(ms, int)
    assert 40 <= ms <= 500


def test_duration_persisted_and_projected() -> None:
    """assistant 消息带耗时、用户消息不带；投影到 Timeline 后仍在。"""

    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        store = SessionStore(db)
        await store.add_user_message("u1", "s1", "帮我分析这个项目")
        await store.add_assistant_message("u1", "s1", "好的", "推理", None, None, 1234)

        records = await store.get_records("u1", "s1")
        assert [r.duration_ms for r in records] == [None, 1234]

        timeline = _timeline(records)
        assistant = [b for b in timeline.blocks if b.type == "assistant"]
        assert assistant[-1].duration_ms == 1234
        await db.close()

    asyncio.run(go())


def test_migration_adds_duration_column_to_legacy_table() -> None:
    """模拟旧库（无 duration_ms）：create_schema 后该列应存在且可写。"""

    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        # 旧版表结构：没有 duration_ms / workspace_id / tool_calls
        await db.execute(
            """CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                reasoning TEXT,
                image_ids TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.commit()

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(chat_messages)")
        columns = [row[1] for row in await cursor.fetchall()]
        await cursor.close()
        assert "duration_ms" in columns

        store = SessionStore(db)
        await store.add_assistant_message("u2", "s2", "hi", duration_ms=250)
        records = await store.get_records("u2", "s2")
        assert records[0].duration_ms == 250
        await db.close()

    asyncio.run(go())
