"""任务计划的持久化（每次 run 一份快照）。

为什么是「整表快照」而不是「逐条行」：
    计划是**每次都覆盖的整体状态**，不是需要审计历史的事件流。逐条行会带来
    「哪一行对应哪一版」的一致性问题，而前端只关心「当前这一版是什么」。
    快照 + 单调递增的 revision 足以支撑幂等覆盖（乱序到达时丢弃旧版本）。

为什么单独建表而不是塞进 session_meta：
    session_meta 是会话级元数据（归档、工作区归属、task_status），一个会话
    可以有多次 run；计划是 run 级状态，生命周期与 request_id 绑定。
"""

from __future__ import annotations

import json

import aiosqlite


class RunPlanStore:
    """run_plans 表的读写。计划内容的语义在这里之外（agentrun/run_plan.py）。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def save(
        self,
        user_id: str,
        session_id: str,
        request_id: str,
        revision: int,
        items: list[dict],
    ) -> None:
        """UPSERT 整份快照。同一个 request_id 只保留最后一版。"""
        await self._db.execute(
            """
            INSERT INTO run_plans (request_id, user_id, session_id, revision, items_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                revision = excluded.revision,
                items_json = excluded.items_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                request_id,
                user_id,
                session_id,
                revision,
                json.dumps(items, ensure_ascii=False),
            ),
        )
        await self._db.commit()

    async def latest(self, user_id: str, session_id: str) -> dict | None:
        """该会话最近一次计划的快照（供刷新后回填）；没有则 None。"""
        cursor = await self._db.execute(
            """
            SELECT request_id, revision, items_json, updated_at
            FROM run_plans
            WHERE user_id = ? AND session_id = ?
            ORDER BY updated_at DESC, rowid DESC
            LIMIT 1
            """,
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        try:
            items = json.loads(row["items_json"])
        except (json.JSONDecodeError, TypeError):
            # 快照损坏时宁可不显示计划，也不要让整个会话打不开
            return None
        return {
            "request_id": row["request_id"],
            "revision": row["revision"],
            "items": items if isinstance(items, list) else [],
            "updated_at": row["updated_at"] or "",
        }

    async def list_for_session(
        self, user_id: str, session_id: str, limit: int = 50
    ) -> list[dict]:
        """该会话每一轮的计划（按时间正序）。

        为什么不是「只要最近一份」：计划是 run 级状态，一轮一份。只取最近一份
        会让新的一轮把上一轮的计划顶掉——界面上就变成「上一轮的计划跑到这一轮
        下面」，或者直接消失。按 request_id 逐轮取回，前端才能把它挂在各自的
        轮次上，互不覆盖。
        """
        cursor = await self._db.execute(
            """
            SELECT request_id, revision, items_json, updated_at
            FROM run_plans
            WHERE user_id = ? AND session_id = ?
            ORDER BY updated_at ASC, rowid ASC
            LIMIT ?
            """,
            (user_id, session_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        plans: list[dict] = []
        for row in rows:
            try:
                items = json.loads(row["items_json"])
            except (json.JSONDecodeError, TypeError):
                continue  # 单份损坏不影响其它轮次
            if not isinstance(items, list):
                continue
            plans.append(
                {
                    "request_id": row["request_id"],
                    "revision": row["revision"],
                    "items": items,
                    "updated_at": row["updated_at"] or "",
                }
            )
        return plans

    async def delete_for_session(self, user_id: str, session_id: str) -> None:
        await self._db.execute(
            "DELETE FROM run_plans WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.commit()
