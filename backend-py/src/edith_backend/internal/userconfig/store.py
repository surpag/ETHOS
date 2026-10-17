"""用户配置模块共用的持久化辅助。

对应 Go 版 internal/userconfig/bindings.go 中的 ensureUser。
"""

from __future__ import annotations

import aiosqlite


async def ensure_user(db: aiosqlite.Connection, user_id: str) -> None:
    """确保用户记录存在；首次访问会建立空白用户记录。"""
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("user id is required")
    await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    await db.commit()
