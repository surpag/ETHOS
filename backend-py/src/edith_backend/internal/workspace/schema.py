"""workspace 模块的表结构：workspaces + project_memories。

对应 `docs/p1-workspace-volume-design.md`：
- workspaces 含 volume_id/volume_token（懒创建 Volume 的持久化字段）
- project_memories 存项目指令（kind='user'）与自动记忆（kind='auto'）
"""

from __future__ import annotations

import aiosqlite

_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        name         TEXT NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}',
        volume_id    TEXT,
        volume_name  TEXT,
        volume_token TEXT,
        local_root   TEXT,
        local_root_prefix TEXT,
        session_id   TEXT,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS project_memories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'user',
        content      TEXT NOT NULL,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, workspace_id, kind, content)
    )""",
]

# 索引在 _migrate 之后创建：旧库可能缺列（project_id → workspace_id 重命名），
# 迁移完成前不能对未存在的列建索引。
_INDEXES = [
    """CREATE INDEX IF NOT EXISTS workspaces_user_index
        ON workspaces (user_id, workspace_id)""",
    """CREATE INDEX IF NOT EXISTS project_memories_lookup
        ON project_memories (user_id, workspace_id, id)""",
]


async def create_schema(db: aiosqlite.Connection) -> None:
    """创建本模块拥有的所有表（先建表，再迁移，最后建索引）。"""
    for statement in _STATEMENTS:
        await db.execute(statement)
    await _migrate(db)
    for statement in _INDEXES:
        await db.execute(statement)
    await db.commit()


async def _migrate(db: aiosqlite.Connection) -> None:
    """幂等迁移：旧库（projects 表 / project_id 列 / 缺 volume 列）升级。"""
    # 1. 旧 projects 表存在 → 复制数据到 workspaces，再删除旧表
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
    )
    has_old_projects = await cursor.fetchone() is not None
    await cursor.close()
    if has_old_projects:
        await db.execute(
            "INSERT OR IGNORE INTO workspaces (workspace_id, user_id, name, created_at) "
            "SELECT project_id, user_id, name, created_at FROM projects"
        )
        await db.execute("DROP TABLE projects")

    # 2. 旧 project_memories 无 workspace_id 列 → 重命名 project_id → workspace_id
    cursor = await db.execute("PRAGMA table_info(project_memories)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = [row[1] for row in rows]
    if "workspace_id" not in columns and "project_id" in columns:
        await db.execute(
            "ALTER TABLE project_memories RENAME COLUMN project_id TO workspace_id"
        )

    # 3. workspaces 缺列 → 逐个补（metadata / volume_id / volume_token）
    cursor = await db.execute("PRAGMA table_info(workspaces)")
    rows = await cursor.fetchall()
    await cursor.close()
    columns = [row[1] for row in rows]
    if "metadata" not in columns:
        await db.execute(
            "ALTER TABLE workspaces ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
        )
    if "volume_id" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN volume_id TEXT")
    if "volume_name" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN volume_name TEXT")
    if "volume_token" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN volume_token TEXT")
    # 本地目录绑定（方案 B：workspace 直接以用户本地目录为存储根）
    if "local_root" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN local_root TEXT")
    # 本地目录在沙箱文件树中的前缀（如卷里多一层 Data/ 时 = "Data"；空 = 直接对应根）
    if "local_root_prefix" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN local_root_prefix TEXT")
    # 一个 workspace 只允许一个 session（方案 B 决策 1）
    if "session_id" not in columns:
        await db.execute("ALTER TABLE workspaces ADD COLUMN session_id TEXT")
