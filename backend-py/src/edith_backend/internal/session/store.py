"""会话消息的 SQLite 访问器。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import aiosqlite

from .types import MessageRecord


class WorkspaceSessionConflict(Exception):
    """同一工作区已有另一个未归档会话。

    「一个工作区一个会话」由 `session_meta` 上的部分唯一索引保证（见 schema.py），
    这里只是把它翻译成可读的异常，供上层转成业务错误而不是 500。
    """

    def __init__(self, workspace_id: str, existing_session_id: str = "") -> None:
        self.workspace_id = workspace_id
        self.existing_session_id = existing_session_id
        super().__init__(
            f"workspace {workspace_id} already has an active session"
            + (f": {existing_session_id}" if existing_session_id else "")
        )


class SessionStore:
    """持久化每个会话的对话消息。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def add_user_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        image_ids: list[str] | None = None,
        workspace_id: str | None = None,
    ) -> None:
        await self._insert(user_id, session_id, "user", content, "", image_ids or [], workspace_id)

    async def add_assistant_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        reasoning: str = "",
        workspace_id: str | None = None,
        tool_calls: list[dict] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await self._insert(
            user_id,
            session_id,
            "assistant",
            content,
            reasoning,
            [],
            workspace_id,
            tool_calls,
            duration_ms,
        )

    async def _insert(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        reasoning: str,
        image_ids: list[str],
        workspace_id: str | None,
        tool_calls: list[dict] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if workspace_id:
            # 先占住工作区再落消息：占不到就直接失败，不会留下一条「无主」消息。
            await self._claim_workspace(user_id, session_id, workspace_id)
        await self._db.execute(
            """
            INSERT INTO chat_messages (user_id, session_id, role, content, reasoning, image_ids, workspace_id, tool_calls, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                session_id,
                role,
                content,
                reasoning or None,
                json.dumps(image_ids) if image_ids else None,
                workspace_id or None,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                duration_ms,
            ),
        )
        await self._db.commit()

    async def _claim_workspace(
        self, user_id: str, session_id: str, workspace_id: str
    ) -> None:
        """把会话登记到工作区上；被别的会话占用时抛 WorkspaceSessionConflict。

        同一会话重复写消息是幂等的（meta 行已存在且 workspace_id 一致时无事发生）；
        冲突来自部分唯一索引 `session_meta_active_workspace_index` —— 这是并发下
        「先查后写」无法避免的竞态的唯一可靠兜底。
        """
        try:
            await self._db.execute(
                """
                INSERT INTO session_meta (user_id, session_id, archived, workspace_id)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE
                    SET workspace_id = COALESCE(session_meta.workspace_id, excluded.workspace_id)
                    WHERE session_meta.workspace_id IS NULL
                """,
                (user_id, session_id, workspace_id),
            )
            await self._db.commit()
        except sqlite3.IntegrityError as exc:
            existing = await self.find_active_session(user_id, workspace_id)
            raise WorkspaceSessionConflict(workspace_id, existing) from exc

    async def find_active_session(self, user_id: str, workspace_id: str) -> str:
        """返回该工作区当前的活跃会话 ID；没有则返回空串。"""
        cursor = await self._db.execute(
            """
            SELECT session_id FROM session_meta
            WHERE user_id = ? AND workspace_id = ? AND archived = 0
            LIMIT 1
            """,
            (user_id.strip(), workspace_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["session_id"] if row else ""

    async def get_messages(
        self, user_id: str, session_id: str, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        """返回 OpenAI 消息格式列表（供拼装模型上下文）。

        Args:
            after_id: 只返回 id > after_id 的消息，如果为 None 则返回全部。
        """
        if after_id is not None:
            cursor = await self._db.execute(
                "SELECT id, role, content FROM chat_messages "
                "WHERE user_id = ? AND session_id = ? AND id > ? ORDER BY id",
                (user_id, session_id, after_id),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, role, content FROM chat_messages "
                "WHERE user_id = ? AND session_id = ? ORDER BY id",
                (user_id, session_id),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {"id": row["id"], "role": row["role"], "content": row["content"]}
            for row in rows
        ]

    async def get_summary(
        self, user_id: str, session_id: str
    ) -> dict[str, int | str] | None:
        """获取会话摘要，如果不存在则返回 None。"""
        cursor = await self._db.execute(
            "SELECT summarized_until_id, summary FROM session_summaries "
            "WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return {
            "summarized_until_id": row["summarized_until_id"],
            "summary": row["summary"],
        }

    async def save_summary(
        self, user_id: str, session_id: str, summarized_until_id: int, summary: str
    ) -> None:
        """保存或更新会话摘要。若不存在则插入，存在则更新。"""
        await self._db.execute(
            "INSERT OR REPLACE INTO session_summaries "
            "(user_id, session_id, summarized_until_id, summary, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, session_id, summarized_until_id, summary),
        )
        await self._db.commit()

    async def get_records(self, user_id: str, session_id: str) -> list[MessageRecord]:
        """返回完整消息记录（供 Timeline 投影）。"""
        cursor = await self._db.execute(
            """
            SELECT id, role, content, reasoning, image_ids, tool_calls, created_at, duration_ms
            FROM chat_messages WHERE user_id = ? AND session_id = ? ORDER BY id
            """,
            (user_id, session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records: list[MessageRecord] = []
        for row in rows:
            image_ids = json.loads(row["image_ids"]) if row["image_ids"] else []
            tool_calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
            records.append(
                MessageRecord(
                    id=row["id"],
                    role=row["role"],
                    content=row["content"] or "",
                    reasoning=row["reasoning"] or "",
                    image_ids=image_ids,
                    tool_calls=tool_calls,
                    created_at=row["created_at"] or "",
                    duration_ms=row["duration_ms"],
                )
            )
        return records

    async def list_sessions(
        self, user_id: str, include_archived: bool = False
    ) -> list[tuple[str, str, str, str, bool]]:
        """返回用户会话摘要：[(session_id, first_message, updated_at, workspace_id, archived)]。

        first_message 取每个会话第一条 user 消息作为标题候选；
        workspace_id 为 NULL 表示普通对话（不属于任何工作区）；
        archived 为 True 表示已归档（默认排除，include_archived=True 时包含）。
        """
        archived_clause = "" if include_archived else "AND COALESCE(m.archived, 0) = 0"
        cursor = await self._db.execute(
            f"""
            SELECT c.session_id,
                   (SELECT content FROM chat_messages m2
                    WHERE m2.user_id = c.user_id AND m2.session_id = c.session_id
                      AND m2.role = 'user' AND m2.content <> ''
                    ORDER BY m2.id LIMIT 1) AS first_message,
                   MAX(c.created_at) AS updated_at,
                   MAX(c.workspace_id) AS workspace_id,
                   COALESCE(MAX(m.archived), 0) AS archived
            FROM chat_messages c
            LEFT JOIN session_meta m
                ON m.user_id = c.user_id AND m.session_id = c.session_id
            WHERE c.user_id = ?
                {archived_clause}
            GROUP BY c.session_id
            ORDER BY updated_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            (
                row["session_id"],
                row["first_message"] or "",
                row["updated_at"] or "",
                row["workspace_id"] or "",
                bool(row["archived"]),
            )
            for row in rows
        ]

    async def archive(self, user_id: str, session_id: str) -> None:
        """本次新增：归档会话（从侧边栏隐藏，但保留数据）。"""
        await self._db.execute(
            """
            INSERT INTO session_meta (user_id, session_id, archived, archived_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, session_id)
            DO UPDATE SET archived = 1, archived_at = CURRENT_TIMESTAMP
            """,
            (user_id, session_id),
        )
        await self._db.commit()

    async def unarchive(self, user_id: str, session_id: str) -> None:
        """取消归档（恢复会话到侧边栏）。

        若该工作区已有别的活跃会话，取消归档会破坏「一个工作区一个会话」，
        由唯一索引拦下并翻译成 WorkspaceSessionConflict。
        """
        try:
            await self._db.execute(
                """
                UPDATE session_meta SET archived = 0, archived_at = NULL
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            )
            await self._db.commit()
        except sqlite3.IntegrityError as exc:
            workspace_id = await self.workspace_of(user_id, session_id)
            existing = (
                await self.find_active_session(user_id, workspace_id)
                if workspace_id
                else ""
            )
            raise WorkspaceSessionConflict(workspace_id, existing) from exc

    async def workspace_of(self, user_id: str, session_id: str) -> str:
        """会话归属的工作区 ID（普通对话返回空串）。"""
        cursor = await self._db.execute(
            "SELECT workspace_id FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id.strip(), session_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (row["workspace_id"] or "") if row else ""

    # ---- 任务状态（断点续跑的恢复依据，与终止原因解耦）----

    async def set_task_status(self, user_id: str, session_id: str, status: str) -> None:
        """写入任务状态（in_progress/completed/canceled）。

        仅"完成/取消"这类明确业务终态会改走 in_progress；其余异常终止不改状态，
        因此崩溃后状态天然停留在 in_progress，可被恢复逻辑识别。
        """
        await self._db.execute(
            """
            INSERT INTO session_meta (user_id, session_id, task_status)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, session_id)
            DO UPDATE SET task_status = excluded.task_status
            """,
            (user_id, session_id, status),
        )
        await self._db.commit()

    async def get_task_status(self, user_id: str, session_id: str) -> str | None:
        """读取任务状态；NULL 表示从未有 run（视为无需恢复）。"""
        cursor = await self._db.execute(
            "SELECT task_status FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["task_status"] if row else None

    # ---- 执行进度日志（WAL：每次工具调用成功后立即追加，独立于 run 终止）----

    async def append_checkpoint(
        self,
        user_id: str,
        session_id: str,
        step: int,
        tool: str,
        args_digest: str,
        result_digest: str,
        progress: str = "",
    ) -> None:
        """追加一条执行进度。每次工具调用成功后立即写，崩溃时进度不丢。

        progress：结构化进度 JSON（如 read_file 的 {path,startLine,endLine,totalLines}），
        供注入「已读摘要」使用。
        """
        await self._db.execute(
            """
            INSERT INTO run_checkpoints
                (user_id, session_id, step, tool, args_digest, result_digest, progress)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                session_id,
                step,
                tool,
                args_digest,
                result_digest,
                progress or None,
            ),
        )
        await self._db.commit()

    async def load_read_progress(
        self, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """返回该会话所有 read_file 的结构化进度（用于注入「已读摘要」）。"""
        cursor = await self._db.execute(
            "SELECT progress FROM run_checkpoints "
            "WHERE user_id = ? AND session_id = ? "
            "AND progress IS NOT NULL AND progress <> '' ORDER BY id",
            (user_id, session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                records.append(json.loads(row["progress"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return records

    async def load_latest_checkpoint(
        self, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """读取最近一条执行进度；无则返回 None。"""
        cursor = await self._db.execute(
            "SELECT step, tool, args_digest, result_digest, created_at "
            "FROM run_checkpoints WHERE user_id = ? AND session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return {
            "step": row["step"],
            "tool": row["tool"],
            "args_digest": row["args_digest"] or "",
            "result_digest": row["result_digest"] or "",
            "created_at": row["created_at"] or "",
        }

    async def clear_checkpoints(self, user_id: str, session_id: str) -> None:
        """清空执行进度（任务完成或用户取消后调用）。"""
        await self._db.execute(
            "DELETE FROM run_checkpoints WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.commit()

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """本次新增：删除整个会话（消息 + 摘要 + 元数据）。返回是否删除成功。"""
        cursor = await self._db.execute(
            "DELETE FROM chat_messages WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        deleted = cursor.rowcount > 0
        await self._db.execute(
            "DELETE FROM session_summaries WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.execute(
            "DELETE FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.execute(
            "DELETE FROM run_checkpoints WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        await self._db.commit()
        return deleted
