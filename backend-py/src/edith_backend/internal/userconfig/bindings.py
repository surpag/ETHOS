"""渠道绑定：外部渠道账号到 Clerk 用户的映射。

对应 Go 版 internal/userconfig/bindings.go。
"""

from __future__ import annotations

import aiosqlite

from .store import ensure_user


class Bindings:
    """提供外部渠道账号到 Clerk 用户的映射。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def bind(self, channel: str, external_user_id: str, clerk_user_id: str) -> None:
        """保存一个渠道账号的归属。"""
        channel = channel.strip()
        external_user_id = external_user_id.strip()
        clerk_user_id = clerk_user_id.strip()
        if not channel or not external_user_id or not clerk_user_id:
            raise ValueError("channel, external user id, and clerk user id are required")
        await ensure_user(self._db, clerk_user_id)
        await self._db.execute(
            """
            INSERT INTO user_channel_bindings (channel, external_user_id, clerk_user_id)
            VALUES (?, ?, ?)
            ON CONFLICT(channel, external_user_id) DO UPDATE SET clerk_user_id = excluded.clerk_user_id
            """,
            (channel, external_user_id, clerk_user_id),
        )
        await self._db.commit()

    async def to_clerk_user_id(
        self, channel: str, external_user_id: str
    ) -> tuple[str | None, bool]:
        """将渠道账号转换为 Clerk 用户；未绑定时 found 为 False。"""
        channel = channel.strip()
        external_user_id = external_user_id.strip()
        if not channel or not external_user_id:
            raise ValueError("channel and external user id are required")
        cursor = await self._db.execute(
            "SELECT clerk_user_id FROM user_channel_bindings WHERE channel = ? AND external_user_id = ?",
            (channel, external_user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None, False
        return row["clerk_user_id"], True
