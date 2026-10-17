"""模型目录模块的表结构：用户自定义供应商与自定义模型。

内置目录（`catalog.py` 的常量）永远是权威源、只读；本表只承载用户在设置页
新增的「叠加层」。两者通过 ID 前缀隔离（见 `custom.py` 的 `CUSTOM_*_PREFIX`），
因此合并时是 `内置 | 自定义` 的一次集合运算，下游不需要「是不是自定义」的分支。

API Key 不落在本模块：自定义供应商与内置供应商共用 `user_providers`
（`(user_id, provider_id) -> api_key`），凭据的读写始终只有 `userconfig.providers`
这一个出口。
"""

from __future__ import annotations

import aiosqlite

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS user_custom_providers (
        user_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, provider_id)
    )""",
    """CREATE TABLE IF NOT EXISTS user_custom_models (
        user_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        name TEXT NOT NULL,
        api_model TEXT NOT NULL,
        context_window INTEGER NOT NULL,
        vision INTEGER NOT NULL DEFAULT 0,
        base_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, model_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_user_custom_models_provider
        ON user_custom_models (user_id, provider_id)""",
]

# 建表语句之后补的列：老库存在但没有这些列，用 ADD COLUMN 幂等补齐
_ADDED_COLUMNS = [
    ("base_url", "TEXT NOT NULL DEFAULT ''"),
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """创建本模块拥有的表（幂等）。"""
    for statement in _STATEMENTS:
        await db.execute(statement)
    await _migrate(db)
    await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """补齐历史库缺失的列（PRAGMA table_info + 幂等 ALTER，对齐 session/schema.py）。"""
    cursor = await db.execute("PRAGMA table_info(user_custom_models)")
    columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    for name, ddl in _ADDED_COLUMNS:
        if name not in columns:
            await db.execute(f"ALTER TABLE user_custom_models ADD COLUMN {name} {ddl}")
