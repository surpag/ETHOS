"""images 的 SQLite 访问器。

对应 Go 版 internal/images/store.go。
"""

from __future__ import annotations

import aiosqlite

from .types import Image

PENDING = "pending"
READY = "ready"


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS chat_images (
            image_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
            object_key TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.commit()


class ImageRecord:
    def __init__(self, image: Image, user_id: str, session_id: str, object_key: str,
                 size_bytes: int, status: str) -> None:
        self.image = image
        self.user_id = user_id
        self.session_id = session_id
        self.object_key = object_key
        self.size_bytes = size_bytes
        self.status = status


class ImageNotFound(Exception):
    pass


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def insert(self, record: ImageRecord) -> None:
        await self._db.execute(
            "INSERT INTO chat_images (image_id, user_id, session_id, object_key, mime_type, size_bytes, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.image.id, record.user_id, record.session_id, record.object_key,
             record.image.mime_type, record.size_bytes, record.status),
        )
        await self._db.commit()

    async def delete(self, image_id: str) -> None:
        await self._db.execute("DELETE FROM chat_images WHERE image_id = ?", (image_id,))
        await self._db.commit()

    async def mark_ready(self, image_id: str) -> None:
        await self._db.execute("UPDATE chat_images SET status = ? WHERE image_id = ?", (READY, image_id))
        await self._db.commit()

    async def _read(self, query: str, args: tuple) -> ImageRecord:
        cursor = await self._db.execute(query, args)
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ImageNotFound("image not found")
        return ImageRecord(
            image=Image(id=row["image_id"], mime_type=row["mime_type"]),
            user_id=row["user_id"],
            session_id=row["session_id"],
            object_key=row["object_key"],
            size_bytes=row["size_bytes"],
            status=row["status"],
        )

    async def for_user(self, user_id: str, image_id: str, ready_only: bool) -> ImageRecord:
        query = "SELECT image_id, user_id, session_id, object_key, mime_type, size_bytes, status FROM chat_images WHERE image_id = ? AND user_id = ?"
        args: list = [image_id.strip(), user_id.strip()]
        if ready_only:
            query += " AND status = ?"
            args.append(READY)
        return await self._read(query, tuple(args))

    async def for_session(self, user_id: str, session_id: str, image_id: str) -> ImageRecord:
        return await self._read(
            "SELECT image_id, user_id, session_id, object_key, mime_type, size_bytes, status FROM chat_images "
            "WHERE image_id = ? AND user_id = ? AND session_id = ? AND status = ?",
            (image_id.strip(), user_id.strip(), session_id.strip(), READY),
        )
