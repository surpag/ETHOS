"""trace 模块的 SQLite 访问器。

写入策略（`docs/trace-eval-design.md` §3.1 决策 2）：
- `insert_trace` 立即 commit——让「有一次 run 正在进行」立刻可见（排查卡住的前提）。
- `insert_steps` 由 Tracer 按轮批量调用，一次 commit 写一批；
  逐条 commit 会在工具路径上产生双倍 fsync（本库是 DELETE journal 模式），
  而 trace 只承担诊断职责，恢复职责由 `run_checkpoints` 承担。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import aiosqlite

from .types import ERROR, RUNNING, Trace, TraceStep

_TRACE_COLUMNS = (
    "request_id, user_id, session_id, workspace_id, user_input, model_id, "
    "started_at, finished_at, status, end_reason, duration_ms, self_time_ms, "
    "total_tokens, tools_called, degraded, degradations, error_type, "
    "error_message, error_traceback"
)

_STEP_COLUMNS = (
    "request_id, seq, parent_seq, depth, type, name, started_at, duration_ms, "
    "status, error_type, error_message, input_digest, output_digest, "
    "input_len, output_len, metadata"
)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def insert_trace(self, trace: Trace) -> None:
        """写入/覆盖一次 run 的骨架行。

        同一 request_id 重复提交（前端双击、重试）时整体覆盖：先清掉旧步骤，
        避免两次尝试的步骤混在同一棵树上。
        """
        await self._db.execute(
            "DELETE FROM trace_steps WHERE request_id = ?", (trace.request_id,)
        )
        await self._db.execute(
            f"INSERT OR REPLACE INTO traces ({_TRACE_COLUMNS}) "
            f"VALUES ({_placeholders(19)})",
            (
                trace.request_id,
                trace.user_id,
                trace.session_id,
                trace.workspace_id,
                trace.user_input,
                trace.model_id,
                trace.started_at,
                trace.finished_at,
                trace.status,
                trace.end_reason,
                trace.duration_ms,
                trace.self_time_ms,
                trace.total_tokens,
                trace.tools_called,
                int(trace.degraded),
                json.dumps(trace.degradations, ensure_ascii=False)
                if trace.degradations
                else None,
                trace.error_type,
                trace.error_message,
                trace.error_traceback,
            ),
        )
        await self._db.commit()

    async def insert_steps(self, request_id: str, steps: list[TraceStep]) -> None:
        """批量追加步骤（一次 commit）。"""
        if not steps:
            return
        await self._db.executemany(
            f"INSERT INTO trace_steps ({_STEP_COLUMNS}) "
            f"VALUES ({_placeholders(16)})",
            [s.to_row(request_id) for s in steps],
        )
        await self._db.commit()

    async def finish_trace(self, trace: Trace) -> None:
        """收尾：写终态、耗时分解、降级记录与异常信息。"""
        await self._db.execute(
            """UPDATE traces SET finished_at = ?, status = ?, end_reason = ?,
                duration_ms = ?, self_time_ms = ?, total_tokens = ?,
                tools_called = ?, model_id = ?, degraded = ?, degradations = ?,
                error_type = ?, error_message = ?, error_traceback = ?
            WHERE request_id = ?""",
            (
                trace.finished_at,
                trace.status,
                trace.end_reason,
                trace.duration_ms,
                trace.self_time_ms,
                trace.total_tokens,
                trace.tools_called,
                trace.model_id,
                int(trace.degraded),
                json.dumps(trace.degradations, ensure_ascii=False)
                if trace.degradations
                else None,
                trace.error_type,
                trace.error_message,
                trace.error_traceback,
                trace.request_id,
            ),
        )
        await self._db.commit()

    async def cleanup(self, days: int) -> int:
        """清理过期 trace：一般 run 保留 days 天，失败/降级 run 保留 2×days 天。

        返回删除的 trace 条数。
        """
        if days <= 0:
            return 0
        now = datetime.now().astimezone()
        soft = (now - timedelta(days=days)).isoformat(timespec="seconds")
        hard = (now - timedelta(days=days * 2)).isoformat(timespec="seconds")
        cursor = await self._db.execute(
            """SELECT request_id FROM traces
               WHERE (started_at < ? AND status != ? AND degraded = 0)
                  OR started_at < ?""",
            (soft, ERROR, hard),
        )
        ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        if not ids:
            return 0
        marks = _placeholders(len(ids))
        await self._db.execute(
            f"DELETE FROM trace_steps WHERE request_id IN ({marks})", ids
        )
        await self._db.execute(f"DELETE FROM traces WHERE request_id IN ({marks})", ids)
        await self._db.commit()
        return len(ids)

    # ── 查询（供 viewer 使用） ─────────────────────────────────────────

    async def list_traces(
        self, user_id: str | None = None, limit: int = 20, running_only: bool = False
    ) -> list[dict]:
        sql = """SELECT request_id, user_id, session_id, workspace_id, user_input,
                        model_id, started_at, finished_at, status, end_reason,
                        duration_ms, self_time_ms, total_tokens, tools_called,
                        degraded, error_type, error_message
                 FROM traces"""
        where: list[str] = []
        params: list = []
        if user_id:
            where.append("user_id = ?")
            params.append(user_id)
        if running_only:
            where.append("status = ?")
            params.append(RUNNING)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, request_id DESC LIMIT ?"
        params.append(limit)
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_trace(self, request_id: str) -> dict | None:
        cursor = await self._db.execute(
            """SELECT request_id, user_id, session_id, workspace_id, user_input,
                      model_id, started_at, finished_at, status, end_reason,
                      duration_ms, self_time_ms, total_tokens, tools_called,
                      degraded, degradations, error_type, error_message,
                      error_traceback
               FROM traces WHERE request_id = ?""",
            (request_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def get_steps(self, request_id: str) -> list[dict]:
        cursor = await self._db.execute(
            """SELECT seq, parent_seq, depth, type, name, started_at, duration_ms,
                      status, error_type, error_message, input_digest,
                      output_digest, input_len, output_len, metadata
               FROM trace_steps WHERE request_id = ? ORDER BY seq ASC""",
            (request_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def slow_steps(
        self, since: str, min_ms: float, limit: int = 30
    ) -> list[dict]:
        """最慢的步骤（跨 run）——排查「响应慢」。"""
        cursor = await self._db.execute(
            """SELECT s.request_id, s.type, s.name, s.duration_ms, s.status,
                      s.error_message, s.metadata, t.user_id, t.started_at
               FROM trace_steps s JOIN traces t ON t.request_id = s.request_id
               WHERE s.started_at >= ? AND s.duration_ms >= ?
               ORDER BY s.duration_ms DESC LIMIT ?""",
            (since, min_ms, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def step_stats(self, since: str) -> list[dict]:
        """按步骤名称聚合（次数 / 平均 / 最慢）——定位「哪个环节最慢」。"""
        cursor = await self._db.execute(
            """SELECT type, name, COUNT(*) AS calls,
                      ROUND(AVG(duration_ms), 1) AS avg_ms,
                      MAX(duration_ms) AS max_ms,
                      SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS errors
               FROM trace_steps WHERE started_at >= ?
               GROUP BY type, name ORDER BY avg_ms DESC""",
            (ERROR, since),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]
