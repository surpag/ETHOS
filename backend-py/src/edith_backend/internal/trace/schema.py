"""trace 模块的表结构。

两张表：`traces`（一次 run 一行）+ `trace_steps`（一步一行）。
与 `usage_runs`（run/行）、`run_checkpoints`（step/行）保持同一范式——
不用 JSON 文件，因为后端可多副本、且崩溃时 JSON 必须结束时一次写会全丢
（见 `docs/trace-eval-design.md` §3.1 决策 1）。
"""

from __future__ import annotations

import aiosqlite

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS traces (
        request_id      TEXT PRIMARY KEY,   -- 复用前端 requestId，与 usage_runs 同键
        user_id         TEXT NOT NULL,      -- 多租户隔离：查询必须带
        session_id      TEXT NOT NULL,
        workspace_id    TEXT,
        user_input      TEXT,               -- 摘要（默认 digest，非原文）
        model_id        TEXT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,               -- NULL = 未收尾（进行中 / 崩溃）
        status          TEXT NOT NULL,      -- running / ok / canceled / error
        end_reason      TEXT,               -- completed / rounds_exhausted / ... (11 种)
        duration_ms     INTEGER,            -- 墙钟耗时
        self_time_ms    INTEGER,            -- 顶层 step 耗时之和
        total_tokens    INTEGER,
        tools_called    INTEGER,
        degraded        INTEGER NOT NULL DEFAULT 0,
        degradations    TEXT,               -- JSON 数组：降级链记录
        error_type      TEXT,
        error_message   TEXT,
        error_traceback TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS trace_steps (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id    TEXT NOT NULL,
        seq           INTEGER NOT NULL,     -- 进入顺序（父 < 子）
        parent_seq    INTEGER,              -- NULL = 顶层；由 ContextVar 自动填
        depth         INTEGER NOT NULL DEFAULT 0,
        type          TEXT NOT NULL,
        name          TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        duration_ms   REAL,
        status        TEXT NOT NULL,        -- ok / error
        error_type    TEXT,
        error_message TEXT,
        input_digest  TEXT,
        output_digest TEXT,
        input_len     INTEGER,
        output_len    INTEGER,
        metadata      TEXT                  -- JSON：tokens / ttft / round / attempts...
    )""",
]

_INDEXES = [
    """CREATE INDEX IF NOT EXISTS traces_user_index
        ON traces (user_id, started_at DESC)""",
    """CREATE INDEX IF NOT EXISTS trace_steps_request_index
        ON trace_steps (request_id, seq)""",
    """CREATE INDEX IF NOT EXISTS trace_steps_started_index
        ON trace_steps (started_at)""",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """建表 + 建索引（幂等）。"""
    for statement in _STATEMENTS:
        await db.execute(statement)
    for statement in _INDEXES:
        await db.execute(statement)
    await db.commit()
