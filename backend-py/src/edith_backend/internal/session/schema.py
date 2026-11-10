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
        completion_evidence TEXT,  -- mutated/external/read_only/none；NULL = 未知（旧行）
        end_reason TEXT,   -- 终止原因（completed/rounds_exhausted/...）；NULL = 旧行未知
        request_id TEXT,   -- 产生这条回复的 run；计划/证据按 run 归档，靠它对上号
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
    # 用户约束（历史之外的一层）：指令性语句进来就会被压缩改写成陈述句，
    # 「不要用 Go」→「讨论了语言选型」== 约束失效。所以约束单独存、原文不改写、
    # 每次 run 重新注入 system prompt，不参与摘要流水线。
    """CREATE TABLE IF NOT EXISTS constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,            -- session / workspace / user
    scope_id TEXT NOT NULL,         -- session_id / workspace_id / ''（user 级）
    text TEXT NOT NULL,             -- 用户原话，禁止改写
    kind TEXT NOT NULL DEFAULT 'soft',        -- hard（必须）/ soft（偏好）
    source TEXT NOT NULL DEFAULT 'user_explicit',
    source_message_id INTEGER,      -- 溯源：可回查原话
    status TEXT NOT NULL DEFAULT 'active',    -- active / superseded / revoked
    superseded_by INTEGER,
    priority INTEGER NOT NULL DEFAULT 100,    -- 超预算时按此截断
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
    # 任务计划（每次 run 一份快照）：整表存快照、不做逐条行。
    # revision 单调递增，前端据此幂等覆盖（乱序到达时丢弃旧 revision）。
    # 存的是**最终态**，不是事件流——事件流由 SSE 负责，这里只管「刷新后还能看到什么」。
    """CREATE TABLE IF NOT EXISTS run_plans (
    request_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
]

# 工作区归属索引：NULL = 普通对话（不属于任何工作区）
_INDEXES = [
    """CREATE INDEX IF NOT EXISTS chat_messages_workspace_index
        ON chat_messages (user_id, workspace_id, id)""",
    """CREATE INDEX IF NOT EXISTS run_checkpoints_session_index
        ON run_checkpoints (user_id, session_id, id)""",
    """CREATE INDEX IF NOT EXISTS constraints_scope_index
        ON constraints (user_id, scope, scope_id, status)""",
    # 去重必须在数据库层：应用层的「先查再插」在并发下必然漏（两个 run 同时
    # 抽取同一句话）。部分索引只约束 active 行，所以「撤销后重新加回」仍然成立。
    """CREATE UNIQUE INDEX IF NOT EXISTS constraints_active_text_index
        ON constraints (user_id, scope, scope_id, text)
        WHERE status = 'active'""",
    # 历史回填只需要「这个会话最近一次计划」，按 updated_at 倒序取第一条
    """CREATE INDEX IF NOT EXISTS run_plans_session_index
        ON run_plans (user_id, session_id, updated_at DESC)""",
    # 一次 run 只有一条助手消息：运行中先落一行、终态再写同一行。
    #
    # 这条唯一索引是「写」的幂等保证——没有它，upsert 只能退化成「先查再写」，
    # 而运行中的进度写入与终态写入来自同一个任务的不同时刻，一旦重入就会出现
    # 两条助手消息（刷新后一步回复变成两步）。部分索引排除 request_id IS NULL
    # 的旧行与用户消息，因此历史数据不需要迁移。
    """CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_assistant_run_index
        ON chat_messages (user_id, session_id, role, request_id)
        WHERE request_id IS NOT NULL""",
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
    if "completion_evidence" not in columns:
        # 可空且无默认值：NULL = 该列引入前的旧行（来源未知），
        # 不要给 DEFAULT，否则等于给历史数据盖一个「已核实」的章。
        await db.execute(
            "ALTER TABLE chat_messages ADD COLUMN completion_evidence TEXT"
        )
    if "end_reason" not in columns:
        # 终止原因（诊断用）。同样可空无默认：NULL = 该列引入前的旧行，
        # 前端据此不显示任何「未完成」标记，而不是把历史消息全判成没做完。
        await db.execute("ALTER TABLE chat_messages ADD COLUMN end_reason TEXT")
    if "request_id" not in columns:
        # 产生这条回复的 run。计划是按 request_id 存的（run_plans），没有这一列
        # 就只能把「这个会话最近一次计划」挂在时间线末尾——那会让上一轮的计划
        # 在新一轮下面继续显示，看起来像是属于新消息。可空无默认：旧行无法追溯，
        # 前端对其不做计划定位（退化为按会话展示）。
        await db.execute("ALTER TABLE chat_messages ADD COLUMN request_id TEXT")

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
