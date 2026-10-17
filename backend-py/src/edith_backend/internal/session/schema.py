"""会话消息的表结构。"""

from __future__ import annotations

import aiosqlite

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        reasoning TEXT,
        image_ids TEXT,
        tool_calls TEXT,   -- 本轮工具调用记录（JSON），供历史 Timeline 展示
        duration_ms INTEGER,  -- 生成本条 assistant 消息的耗时（毫秒）；用户消息为 NULL
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS chat_messages_session_index
        ON chat_messages (user_id, session_id, id)""",
    """CREATE TABLE IF NOT EXISTS session_summaries (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summarized_until_id INTEGER NOT NULL,   -- 已折叠到的最后一条消息 id
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, session_id)
)""",
    # 本次新增：会话元数据（归档状态等）。会话是隐式的（按 session_id 分组消息），
    # 归档状态单独存这里，避免污染消息表。
    """CREATE TABLE IF NOT EXISTS session_meta (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    task_status TEXT,   -- 任务状态：in_progress/completed/canceled（NULL = 从未有 run）
    PRIMARY KEY (user_id, session_id)
)""",
    # 本次新增：断点续跑进度日志（WAL 思路）——每次工具调用成功后立即追加一条，
    # 独立于 run 终止，崩溃时进度不丢。恢复依据只读这张表 + task_status。
    """CREATE TABLE IF NOT EXISTS run_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    step INTEGER NOT NULL,          -- 第几次工具调用
    tool TEXT NOT NULL,
    args_digest TEXT,               -- 参数摘要
    result_digest TEXT,             -- 结果摘要
    progress TEXT,                  -- 结构化进度（JSON）：如 read_file 的 {path,startLine,endLine,totalLines}
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
]

# 工作区归属索引：NULL = 普通对话（不属于任何工作区）
_INDEXES = [
    """CREATE INDEX IF NOT EXISTS chat_messages_workspace_index
        ON chat_messages (user_id, workspace_id, id)""",
    """CREATE INDEX IF NOT EXISTS run_checkpoints_session_index
        ON run_checkpoints (user_id, session_id, id)""",
]

# 「一个工作区只能有一个未归档会话」的数据库不变式。
#
# 为什么必须是结构约束而不是并发闸门：每个会话持有自己的沙箱，而 B2/B3 都以
# 「本地目录 ↔ 沙箱」为镜像轴。同工作区两个会话即使**不并发**，旧沙箱下次被唤醒时
# 也会把过期内容同步回去（后同步者胜，另一份改动静默丢失）。
# `agentrun/workspace_lanes.py` 只挡住了「同时使用」，挡不住这种顺序覆盖。
# 放在唯一索引里，Web / IM / 定时任务 / 直连 API 都绕不过去。
#
# 部分索引条件 archived = 0：归档的会话保留数据但退出索引，所以「归档 → 新开」
# 这条路径仍然成立。workspace_id IS NULL 排除普通对话（它们可自由多开）。
_UNIQUE_ACTIVE_SESSION_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS session_meta_active_workspace_index
    ON session_meta (user_id, workspace_id)
    WHERE archived = 0 AND workspace_id IS NOT NULL
"""

# 把 chat_messages 上的工作区归属回填到 session_meta（会话元数据的唯一来源）。
# 只在 meta 的 workspace_id 为空时写入，避免覆盖已有值。
_BACKFILL_WORKSPACE = """
INSERT INTO session_meta (user_id, session_id, archived, workspace_id)
SELECT c.user_id, c.session_id, 0, MAX(c.workspace_id)
FROM chat_messages AS c
WHERE c.workspace_id IS NOT NULL
GROUP BY c.user_id, c.session_id
ON CONFLICT(user_id, session_id) DO UPDATE
    SET workspace_id = COALESCE(session_meta.workspace_id, excluded.workspace_id)
    WHERE session_meta.workspace_id IS NULL
"""


async def create_schema(db: aiosqlite.Connection) -> None:
    """创建会话消息表（先建表，再迁移补列，最后建索引与不变式）。"""
    for statement in _STATEMENTS:
        await db.execute(statement)
    await _migrate(db)
    for statement in _INDEXES:
        await db.execute(statement)
    await _enforce_single_session_per_workspace(db)
    await db.commit()


async def _enforce_single_session_per_workspace(db: aiosqlite.Connection) -> None:
    """回填工作区归属 → 收敛历史上的重复会话 → 建唯一索引。

    顺序不能换：索引必须在数据收敛之后建，否则老库里已有的重复会话会让建索引失败。
    整个过程幂等，每次启动都可以安全重跑。
    """
    await db.execute(_BACKFILL_WORKSPACE)
    await _archive_duplicate_workspace_sessions(db)
    await db.execute(_UNIQUE_ACTIVE_SESSION_INDEX)


async def _archive_duplicate_workspace_sessions(db: aiosqlite.Connection) -> None:
    """同一工作区有多个未归档会话时，只保留**最近有消息**的那个，其余归档。

    归档不删数据：被归档的会话仍可从侧边栏的归档区找回，只是不再占用工作区。
    """
    cursor = await db.execute(
        """
        SELECT user_id, session_id FROM (
            SELECT m.user_id, m.session_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.user_id, m.workspace_id
                       ORDER BY COALESCE((
                           SELECT MAX(c.id) FROM chat_messages AS c
                           WHERE c.user_id = m.user_id AND c.session_id = m.session_id
                       ), 0) DESC
                   ) AS rn
            FROM session_meta AS m
            WHERE m.archived = 0 AND m.workspace_id IS NOT NULL
        ) WHERE rn > 1
        """
    )
    duplicates = await cursor.fetchall()
    await cursor.close()
    for row in duplicates:
        await db.execute(
            """
            UPDATE session_meta SET archived = 1, archived_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND session_id = ?
            """,
            (row[0], row[1]),
        )


async def _migrate(db: aiosqlite.Connection) -> None:
    """幂等迁移：给 chat_messages 补 workspace_id / tool_calls / duration_ms 列等。

    旧库缺少对应列时 ALTER TABLE 补齐。
    """
    cursor = await db.execute("PRAGMA table_info(chat_messages)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = [row[1] for row in rows]
    if "workspace_id" not in columns:
        await db.execute(
            "ALTER TABLE chat_messages ADD COLUMN workspace_id TEXT"
        )
    if "tool_calls" not in columns:
        await db.execute("ALTER TABLE chat_messages ADD COLUMN tool_calls TEXT")
    if "duration_ms" not in columns:
        await db.execute("ALTER TABLE chat_messages ADD COLUMN duration_ms INTEGER")

    cursor = await db.execute("PRAGMA table_info(session_meta)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = [row[1] for row in rows]
    if "task_status" not in columns:
        await db.execute("ALTER TABLE session_meta ADD COLUMN task_status TEXT")
    if "workspace_id" not in columns:
        # 会话的工作区归属从 chat_messages 提升到会话元数据：单一权威源在这里，
        # 否则「一个工作区一个会话」不变式无法用索引表达。
        await db.execute("ALTER TABLE session_meta ADD COLUMN workspace_id TEXT")

    cursor = await db.execute("PRAGMA table_info(run_checkpoints)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = [row[1] for row in rows]
    if columns and "progress" not in columns:
        await db.execute("ALTER TABLE run_checkpoints ADD COLUMN progress TEXT")
