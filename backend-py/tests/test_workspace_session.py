"""「一个工作区一个会话」不变式的验收测试。

背景：每个会话持有自己的沙箱，而 B2/B3 以「本地目录 ↔ 沙箱」为镜像轴。
同工作区两个会话即使不并发，旧沙箱下次被唤醒时也会把过期内容同步回去
（后同步者胜，另一份改动静默丢失）。所以这不是 UI 偏好，是数据结构约束。

覆盖：占位成功 / 冲突拒绝 / 同会话幂等 / 归档释放 / 取消归档冲突 /
普通对话不受限 / 历史数据收敛 / 迁移幂等 / HTTP 路由可达。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from edith_backend.database import open_database
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore, WorkspaceSessionConflict

WORKSPACE = "ws-alpha"
OTHER_WORKSPACE = "ws-beta"


def _run(scenario):
    return asyncio.run(scenario)


def _scenario(body):
    """把一个 async 场景包成同步测试，并负责建库/关库。"""

    async def runner():
        db = await open_database(":memory:")
        try:
            await create_schema(db)
            return await body(SessionStore(db), db)
        finally:
            await db.close()

    return _run(runner())


def test_first_session_claims_the_workspace() -> None:
    async def body(store: SessionStore, db) -> None:
        await store.add_user_message("u1", "s1", "hello", None, WORKSPACE)
        assert await store.find_active_session("u1", WORKSPACE) == "s1"

    _scenario(body)


def test_second_session_in_same_workspace_is_rejected() -> None:
    async def body(store: SessionStore, db) -> None:
        await store.add_user_message("u1", "s1", "first", None, WORKSPACE)
        with pytest.raises(WorkspaceSessionConflict) as caught:
            await store.add_user_message("u1", "s2", "second", None, WORKSPACE)
        # 冲突要带上「谁占着」，前端据此直接切过去
        assert caught.value.existing_session_id == "s1"
        assert caught.value.workspace_id == WORKSPACE
        # 失败不能留下半截消息
        messages = await store.get_messages("u1", "s2")
        assert messages == []

    _scenario(body)


def test_same_session_can_keep_writing() -> None:
    async def body(store: SessionStore, db) -> None:
        for text in ("one", "two", "three"):
            await store.add_user_message("u1", "s1", text, None, WORKSPACE)
        await store.add_assistant_message("u1", "s1", "reply", workspace_id=WORKSPACE)
        records = await store.get_records("u1", "s1")
        assert len(records) == 4

    _scenario(body)


def test_archiving_frees_the_workspace_for_a_new_session() -> None:
    async def body(store: SessionStore, db) -> None:
        await store.add_user_message("u1", "s1", "first", None, WORKSPACE)
        await store.archive("u1", "s1")
        assert await store.find_active_session("u1", WORKSPACE) == ""

        # 归档后可以开新会话（「重开一个」的合法路径）
        await store.add_user_message("u1", "s2", "second", None, WORKSPACE)
        assert await store.find_active_session("u1", WORKSPACE) == "s2"

        # 旧会话的数据还在（只是从列表隐藏），没被删
        assert len(await store.get_records("u1", "s1")) == 1

    _scenario(body)


def test_unarchive_conflicts_when_workspace_is_taken() -> None:
    async def body(store: SessionStore, db) -> None:
        await store.add_user_message("u1", "s1", "first", None, WORKSPACE)
        await store.archive("u1", "s1")
        await store.add_user_message("u1", "s2", "second", None, WORKSPACE)

        with pytest.raises(WorkspaceSessionConflict) as caught:
            await store.unarchive("u1", "s1")
        assert caught.value.existing_session_id == "s2"

        # 先归档当前会话，再恢复旧会话 —— 顺序正确时允许
        await store.archive("u1", "s2")
        await store.unarchive("u1", "s1")
        assert await store.find_active_session("u1", WORKSPACE) == "s1"

    _scenario(body)


def test_plain_conversations_are_not_limited() -> None:
    async def body(store: SessionStore, db) -> None:
        # 无工作区的普通对话不受约束（没有共享目录，也就不存在镜像覆盖）
        for index in range(3):
            await store.add_user_message("u1", f"n{index}", "hi", None, None)
        sessions = await store.list_sessions("u1")
        assert len(sessions) == 3
        assert all(row[3] == "" for row in sessions)

    _scenario(body)


def test_workspaces_are_independent() -> None:
    async def body(store: SessionStore, db) -> None:
        await store.add_user_message("u1", "s1", "a", None, WORKSPACE)
        await store.add_user_message("u1", "s2", "b", None, OTHER_WORKSPACE)
        assert await store.find_active_session("u1", WORKSPACE) == "s1"
        assert await store.find_active_session("u1", OTHER_WORKSPACE) == "s2"
        # 另一个用户也可以用同名工作区（userId 是隔离维度）
        await store.add_user_message("u2", "s3", "c", None, WORKSPACE)
        assert await store.find_active_session("u2", WORKSPACE) == "s3"

    _scenario(body)


def test_migration_collapses_duplicate_workspace_sessions() -> None:
    """老库：同一工作区已有多个会话 → 只保留最近有消息的，其余自动归档。"""

    async def runner() -> dict:
        db = await open_database(":memory:")
        try:
            await db.execute(
                """CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    reasoning TEXT,
                    image_ids TEXT,
                    tool_calls TEXT,
                    duration_ms INTEGER,
                    workspace_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            await db.execute(
                """CREATE TABLE session_meta (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    archived_at TEXT,
                    task_status TEXT,
                    PRIMARY KEY (user_id, session_id)
                )"""
            )
            # old 更早、new 更晚；plain 是普通对话
            for session_id, text in (("old", "a"), ("new", "b")):
                await db.execute(
                    "INSERT INTO chat_messages (user_id, session_id, role, content, workspace_id) "
                    "VALUES (?, ?, 'user', ?, ?)",
                    ("u1", session_id, text, WORKSPACE),
                )
            await db.execute(
                "INSERT INTO chat_messages (user_id, session_id, role, content) "
                "VALUES ('u1', 'plain', 'user', 'hi')"
            )

            await create_schema(db)  # 迁移 + 收敛 + 唯一索引

            result = {}
            for session_id in ("old", "new", "plain"):
                cursor = await db.execute(
                    "SELECT archived, workspace_id FROM session_meta "
                    "WHERE user_id = 'u1' AND session_id = ?",
                    (session_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                # 普通对话不回填 meta（meta 缺失 = 未归档 + 无工作区），因此为 None
                result[session_id] = None if row is None else (row["archived"], row["workspace_id"])
            return result
        finally:
            await db.close()

    result = _run(runner())
    assert result["new"] == (0, WORKSPACE)      # 最近有消息的保留
    assert result["old"] == (1, WORKSPACE)      # 更早的归档（数据保留）
    assert result["plain"] is None              # 普通对话不进 meta，也不受约束


def test_migration_is_idempotent() -> None:
    """连跑两次不能把唯一的活跃会话也归档掉。"""

    async def runner() -> int:
        db = await open_database(":memory:")
        try:
            await create_schema(db)
            store = SessionStore(db)
            await store.add_user_message("u1", "s1", "hi", None, WORKSPACE)
            await create_schema(db)
            await create_schema(db)
            assert await store.find_active_session("u1", WORKSPACE) == "s1"
            return 1
        finally:
            await db.close()

    assert _run(runner()) == 1


def test_by_workspace_endpoint_is_reachable(client: TestClient) -> None:
    """静态子路径不能被 /{session_id} 吞掉（曾经拼错路径就是这么坏的）。"""
    response = client.get(
        "/internal/conversations/by-workspace/never-used-workspace",
        params={"userId": "test-workspace-session-route"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sessionId"] == ""
