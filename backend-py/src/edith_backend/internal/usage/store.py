"""usage 模块的 SQLite 访问器。

对应 Go 版 internal/usage/store.go。
"""

from __future__ import annotations

import aiosqlite

from .types import Run, Summary, Tokens

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"


class RunAlreadyExists(Exception):
    """同一个 requestID 已被记录。"""


class RunNotFound(Exception):
    """找不到指定的运行记录。"""


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create_schema(self) -> None:
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS usage_runs (
                request_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt_tokens INTEGER,
                cached_prompt_tokens INTEGER,
                cache_reported INTEGER NOT NULL DEFAULT 0,
                -- NULL = 旧行（该列引入前写入），无法判断来源，不做猜测
                usage_reported INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )"""
        )
        # 增量列（幂等）：usage_reported 用于区分「供应商真实上报」与「本地估算」，
        # 旧库可能没有该列，按 session/schema.py 的既有先例补列。
        cursor = await self._db.execute("PRAGMA table_info(usage_runs)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "usage_reported" not in columns:
            # 不加 DEFAULT：旧行填 NULL 表示「来源未记录」。若默认 1（上报）会把
            # 历史估算值冒充成供应商真实上报，正是这次要修的那类假精度。
            await self._db.execute(
                "ALTER TABLE usage_runs ADD COLUMN usage_reported INTEGER"
            )
        await self._db.execute(
            """CREATE INDEX IF NOT EXISTS usage_runs_session_index
                ON usage_runs (user_id, session_id, status)"""
        )
        await self._db.commit()

    async def start(self, run: Run) -> None:
        if not run.request_id or not run.user_id or not run.session_id or not run.model_id:
            raise ValueError("requestID, userID, sessionID, and modelID are required")
        cursor = await self._db.execute(
            """
            INSERT INTO usage_runs (request_id, user_id, session_id, model_id, status)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(request_id) DO NOTHING
            """,
            (run.request_id, run.user_id, run.session_id, run.model_id, RUNNING),
        )
        if cursor.rowcount != 1:
            raise RunAlreadyExists("agent run already exists")
        await self._db.commit()

    async def finish(self, run: Run, tokens: Tokens) -> None:
        cached = tokens.cached_prompt_tokens
        cache_reported = 1 if cached is not None else 0
        cursor = await self._db.execute(
            """
            UPDATE usage_runs
            SET prompt_tokens = ?, cached_prompt_tokens = ?, cache_reported = ?,
                usage_reported = ?, completion_tokens = ?, total_tokens = ?,
                status = ?, completed_at = CURRENT_TIMESTAMP
            WHERE request_id = ? AND status = ?
            """,
            (
                tokens.prompt_tokens,
                cached,
                cache_reported,
                1 if tokens.reported else 0,
                tokens.completion_tokens,
                tokens.total_tokens,
                COMPLETED,
                run.request_id,
                RUNNING,
            ),
        )
        if cursor.rowcount != 1:
            raise RunNotFound(f"finish agent run {run.request_id}: agent run not found")
        await self._db.commit()

    async def fail(self, request_id: str) -> None:
        await self._db.execute(
            "UPDATE usage_runs SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE request_id = ? AND status = ?",
            (FAILED, request_id, RUNNING),
        )
        await self._db.commit()

    async def status(self, user_id: str, request_id: str) -> str:
        cursor = await self._db.execute(
            "SELECT status FROM usage_runs WHERE user_id = ? AND request_id = ?",
            (user_id, request_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RunNotFound("agent run not found")
        return row["status"]

    async def session_summary(self, user_id: str, session_id: str) -> Summary:
        # 缓存字段只在「自己上报过缓存」的运行上聚合：
        #   cache_prompt = 这些运行的 prompt 之和（命中率的分母必须与分子同源）
        # 旧实现要求 cache_known == count，一次估算就把整个会话的缓存统计清零。
        cursor = await self._db.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(total_tokens), 0) AS total,
                   COALESCE(SUM(completion_tokens), 0) AS completion,
                   COALESCE(SUM(cache_reported), 0) AS cache_known,
                   COALESCE(SUM(CASE WHEN usage_reported = 1 THEN 1 ELSE 0 END), 0)
                       AS reported_runs,
                   COALESCE(SUM(CASE WHEN usage_reported = 0 THEN 1 ELSE 0 END), 0)
                       AS estimated_runs,
                   COALESCE(SUM(CASE WHEN cache_reported = 1 THEN prompt_tokens ELSE 0 END), 0)
                       AS cache_prompt,
                   SUM(CASE WHEN cache_reported = 1 THEN cached_prompt_tokens ELSE NULL END)
                       AS cached
            FROM usage_runs WHERE user_id = ? AND session_id = ? AND status = ?
            """,
            (user_id, session_id, COMPLETED),
        )
        row = await cursor.fetchone()
        await cursor.close()

        total_runs = int(row["count"] or 0)
        cache_known = int(row["cache_known"] or 0)
        summary = Summary(
            total_tokens=int(row["total"] or 0),
            completion_tokens=int(row["completion"] or 0),
            total_runs=total_runs,
            cache_reported_runs=cache_known,
            estimated_runs=int(row["estimated_runs"] or 0),
        )
        if cache_known == 0:
            return summary
        cached = int(row["cached"] or 0)
        cache_prompt = int(row["cache_prompt"] or 0)
        summary.cached_prompt_tokens = cached
        summary.uncached_prompt_tokens = cache_prompt - cached
        summary.cache_hit_rate = (cached / cache_prompt) if cache_prompt > 0 else 0.0
        return summary
