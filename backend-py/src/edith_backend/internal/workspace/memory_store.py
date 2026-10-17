"""项目指令（kind='user'）与自动记忆（kind='auto'）的 SQLite 访问器。

对应 `docs/p0-phase-a-design.md`：
- kind='user'：用户写的工作区指令（整段覆盖式保存，等价 AGENTS.md）
- kind='auto'：AI 自动总结的记忆（Phase B 预留，多条）
记忆以 (user_id, workspace_id) 为粒度，多用户天然隔离。
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

KIND_USER = "user"
KIND_AUTO = "auto"

# 注入上限：对齐 wuu MEMORY.md ≤200 行，防记忆撑爆上下文
INJECT_LIMIT = 200


class WorkspaceMemoryStore:
    """提供工作区指令与自动记忆的读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ---- 工作区指令（kind='user'，整段覆盖） ----

    async def save_instruction(self, user_id: str, workspace_id: str, content: str) -> None:
        """整段覆盖保存工作区指令：先删旧指令，再插入新内容（事务内原子）。"""
        content = content.strip()
        await self._db.execute(
            "DELETE FROM project_memories WHERE user_id = ? AND workspace_id = ? AND kind = ?",
            (user_id, workspace_id, KIND_USER),
        )
        if content:
            await self._db.execute(
                "INSERT INTO project_memories (user_id, workspace_id, kind, content) "
                "VALUES (?, ?, ?, ?)",
                (user_id, workspace_id, KIND_USER, content),
            )
        await self._db.commit()

    async def load_instruction(self, user_id: str, workspace_id: str) -> str:
        """读取工作区指令（整段文本，无则空串）。"""
        async with self._db.execute(
            "SELECT content FROM project_memories "
            "WHERE user_id = ? AND workspace_id = ? AND kind = ? ORDER BY id LIMIT 1",
            (user_id, workspace_id, KIND_USER),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else ""

    # ---- 自动记忆（kind='auto'，多条） ----

    async def add(self, user_id: str, workspace_id: str, content: str, kind: str = KIND_AUTO) -> int:
        """新增一条记忆；UNIQUE 冲突（已存在同内容同 kind）时返回已有 id（幂等）。"""
        try:
            async with self._db.execute(
                "INSERT INTO project_memories (user_id, workspace_id, kind, content) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (user_id, workspace_id, kind, content),
            ) as cursor:
                row = await cursor.fetchone()
                await self._db.commit()
                return row[0]
        except aiosqlite.IntegrityError:
            async with self._db.execute(
                "SELECT id FROM project_memories "
                "WHERE user_id = ? AND workspace_id = ? AND kind = ? AND content = ?",
                (user_id, workspace_id, kind, content),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0]

    async def list(self, user_id: str, workspace_id: str, kind: str = KIND_AUTO) -> list[dict]:
        """列出工作区记忆（默认自动记忆；可按 kind 过滤）。"""
        async with self._db.execute(
            "SELECT id, content FROM project_memories "
            "WHERE user_id = ? AND workspace_id = ? AND kind = ? ORDER BY id",
            (user_id, workspace_id, kind),
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"id": r[0], "content": r[1]} for r in rows]

    async def delete(self, memory_id: int, user_id: str) -> bool:
        """删除一条记忆（同时校验 user_id 防止越权）。"""
        async with self._db.execute(
            "DELETE FROM project_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        ) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    async def load_injectable(self, user_id: str, workspace_id: str) -> str:
        """拼装注入文本：工作区指令（优先）+ 自动记忆（补充）。"""
        parts: list[str] = []
        instruction = await self.load_instruction(user_id, workspace_id)
        if instruction:
            parts.append(instruction)
        auto_rows = await self.list(user_id, workspace_id, KIND_AUTO)
        if auto_rows:
            lines = [f"- {r['content']}" for r in auto_rows[:INJECT_LIMIT]]
            if len(auto_rows) > INJECT_LIMIT:
                logger.warning(
                    "auto memory truncated user=%s workspace=%s limit=%d",
                    user_id, workspace_id, INJECT_LIMIT,
                )
            parts.append("【自动记忆】\n" + "\n".join(lines))
        return "\n\n".join(parts)
