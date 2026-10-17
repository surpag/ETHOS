"""sandbox 模块的表结构。

对应 Go 版 internal/sandbox/schema.go。
"""

from __future__ import annotations

import aiosqlite


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS user_sandboxes (
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sandbox_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, session_id)
        )"""
    )
    await db.commit()
