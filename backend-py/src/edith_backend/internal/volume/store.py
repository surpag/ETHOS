"""volume 模块的表结构与 SQLite 访问器。

对应 Go 版 internal/volume/store.go。
"""

from __future__ import annotations

import aiosqlite


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS user_volumes (
            user_id TEXT NOT NULL PRIMARY KEY,
            volume_id TEXT NOT NULL UNIQUE,
            volume_name TEXT NOT NULL UNIQUE,
            volume_token TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.commit()


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def load(self, user_id: str) -> tuple[str, str, str] | None:
        """返回 (volume_id, volume_name, volume_token) 或 None。"""
        cursor = await self._db.execute(
            "SELECT volume_id, volume_name, volume_token FROM user_volumes WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return row["volume_id"], row["volume_name"], row["volume_token"]

    async def save(self, user_id: str, volume_id: str, volume_name: str, token: str) -> None:
        await self._db.execute(
            "INSERT INTO user_volumes (user_id, volume_id, volume_name, volume_token) VALUES (?, ?, ?, ?)",
            (user_id, volume_id, volume_name, token),
        )
        await self._db.commit()

    async def update_token(self, user_id: str, token: str) -> None:
        await self._db.execute(
            "UPDATE user_volumes SET volume_token = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (token, user_id),
        )
        await self._db.commit()
