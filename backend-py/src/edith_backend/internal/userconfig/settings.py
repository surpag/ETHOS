"""用户 Agent 设置的读写能力。

对应 Go 版 internal/userconfig/settings.go。
"""

from __future__ import annotations

import aiosqlite

from .store import ensure_user
from .types import AgentSettings


class Settings:
    """提供用户 Agent 设置的读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def load(self, user_id: str) -> AgentSettings:
        """读取用户设置；首次读取会建立空白用户记录。"""
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await ensure_user(self._db, user_id)
        cursor = await self._db.execute(
            "SELECT personality, default_model_id, timezone, git_author_name, git_author_email "
            "FROM user_agents WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return AgentSettings()
        return AgentSettings(
            personality=row["personality"] or "",
            default_model_id=row["default_model_id"] or "",
            timezone=row["timezone"] or "",
            git_author_name=row["git_author_name"] or "",
            git_author_email=row["git_author_email"] or "",
        )

    async def save(self, user_id: str, settings: AgentSettings) -> None:
        """保存人格、默认模型、时区与 git 身份，不处理供应商密钥。"""
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await self._db.execute(
            """
            INSERT INTO user_agents
                (user_id, personality, default_model_id, timezone, git_author_name, git_author_email)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                personality = excluded.personality,
                default_model_id = excluded.default_model_id,
                timezone = excluded.timezone,
                git_author_name = excluded.git_author_name,
                git_author_email = excluded.git_author_email
            """,
            (
                user_id,
                settings.personality.strip(),
                settings.default_model_id.strip(),
                settings.timezone.strip(),
                settings.git_author_name.strip(),
                settings.git_author_email.strip(),
            ),
        )
        await self._db.commit()

    async def load_personality(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.personality

    async def load_default_model_id(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.default_model_id

    async def load_timezone(self, user_id: str) -> str:
        settings = await self.load(user_id)
        return settings.timezone

    async def save_timezone(self, user_id: str, timezone: str) -> None:
        settings = await self.load(user_id)
        settings.timezone = timezone
        await self.save(user_id, settings)
