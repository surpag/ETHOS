"""cronjob 的表结构。

对应 Go 版 internal/cronjob/schema.go。
"""

from __future__ import annotations

import aiosqlite


async def create_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS cron_jobs (
            id TEXT PRIMARY KEY,
            clerk_user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            schedule TEXT NOT NULL,
            prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,
            running INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    await db.commit()
