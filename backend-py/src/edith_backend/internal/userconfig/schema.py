"""用户配置模块的表结构。

对应 Go 版 internal/userconfig/schema.go。
workspaces / project_memories 已迁到 workspace 模块（internal/workspace/schema.py）。
"""

from __future__ import annotations

import aiosqlite

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS user_agents (
        user_id TEXT PRIMARY KEY,
        personality TEXT NOT NULL DEFAULT '',
        default_model_id TEXT NOT NULL DEFAULT '',
        timezone TEXT NOT NULL DEFAULT '',
        git_author_name TEXT NOT NULL DEFAULT '',
        git_author_email TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS user_providers (
        user_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        api_key TEXT NOT NULL,
        PRIMARY KEY (user_id, provider_id)
    )""",
    """CREATE TABLE IF NOT EXISTS user_mcp_servers (
        server_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        transport TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS user_mcp_headers (
        server_id TEXT NOT NULL,
        header_name TEXT NOT NULL,
        header_value TEXT NOT NULL,
        PRIMARY KEY (server_id, header_name)
    )""",
    """CREATE TABLE IF NOT EXISTS user_channel_bindings (
        channel TEXT NOT NULL,
        external_user_id TEXT NOT NULL,
        clerk_user_id TEXT NOT NULL,
        PRIMARY KEY (channel, external_user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS user_git_credentials (
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, provider)
    )""",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """创建本模块拥有的所有表，并补齐历史库缺失的列。"""
    for statement in _STATEMENTS:
        await db.execute(statement)
    await _migrate(db)
    await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """旧库缺少 git 身份列时 ALTER TABLE 补齐（幂等，对齐 session/schema.py 的做法）。"""
    cursor = await db.execute("PRAGMA table_info(user_agents)")
    columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "git_author_name" not in columns:
        await db.execute(
            "ALTER TABLE user_agents ADD COLUMN git_author_name TEXT NOT NULL DEFAULT ''"
        )
    if "git_author_email" not in columns:
        await db.execute(
            "ALTER TABLE user_agents ADD COLUMN git_author_email TEXT NOT NULL DEFAULT ''"
        )
