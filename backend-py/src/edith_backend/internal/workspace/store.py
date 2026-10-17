"""工作区实体的 SQLite 访问器：用户命名，系统生成 workspace_id。

对应 `docs/p1-workspace-volume-design.md`：
- workspaces 含 metadata（文件清单）与 volume_id/volume_token（懒创建 Volume）
- 删除工作区时级联删除指令 + 通过回调销毁 Volume（解耦 E2B）
"""

from __future__ import annotations

import json
from uuid import uuid4

import aiosqlite


class DuplicateWorkspace(Exception):
    """同名工作区已存在。"""


class WorkspaceStore:
    """提供工作区实体的创建、查询、Volume 绑定与级联删除。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def create(
        self,
        user_id: str,
        name: str,
        metadata: dict | None = None,
        local_root: str | None = None,
        session_id: str | None = None,
        local_root_prefix: str | None = None,
    ) -> str:
        """创建工作区并返回系统生成的 workspace_id。metadata 存为 JSON 字符串。

        local_root：绑定本地目录（方案 B）；session_id：绑定会话（决策 1，一 workspace 一 session）。
        local_root_prefix：本地目录在沙箱文件树中的前缀（见 schema.py 注释）。
        """
        workspace_id = uuid4().hex
        try:
            await self._db.execute(
                "INSERT INTO workspaces "
                "(workspace_id, user_id, name, metadata, local_root, session_id, local_root_prefix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    user_id,
                    name,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    local_root or None,
                    session_id or None,
                    local_root_prefix or None,
                ),
            )
            await self._db.commit()
        except aiosqlite.IntegrityError as exc:
            raise DuplicateWorkspace(f"workspace name already exists: {name}") from exc
        return workspace_id

    async def get_local_root(self, user_id: str, workspace_id: str) -> str | None:
        """返回工作区绑定的本地目录（方案 B）；未绑定返回 None。"""
        async with self._db.execute(
            "SELECT local_root FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def get_local_root_prefix(self, user_id: str, workspace_id: str) -> str:
        """返回本地目录在沙箱文件树中的前缀；空串表示直接对应 workspace 根。"""
        async with self._db.execute(
            "SELECT local_root_prefix FROM workspaces "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return (row[0] or "") if row else ""

    async def bind_local_root(
        self,
        user_id: str,
        workspace_id: str,
        local_root: str,
        prefix: str = "",
    ) -> bool:
        """给已有 workspace 绑定本地目录（方案 B：无需重建工作区）。返回是否命中记录。"""
        cursor = await self._db.execute(
            "UPDATE workspaces SET local_root = ?, local_root_prefix = ? "
            "WHERE user_id = ? AND workspace_id = ?",
            (local_root, prefix or None, user_id, workspace_id),
        )
        affected = cursor.rowcount or 0
        await cursor.close()
        await self._db.commit()
        return affected > 0

    async def get_session_id(self, user_id: str, workspace_id: str) -> str | None:
        """返回工作区绑定的会话 id（决策 1）；未绑定返回 None。"""
        async with self._db.execute(
            "SELECT session_id FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def bind_session(
        self, user_id: str, workspace_id: str, session_id: str
    ) -> None:
        """惰性绑定会话（旧 workspace 首次访问时调用）。"""
        await self._db.execute(
            "UPDATE workspaces SET session_id = ? "
            "WHERE user_id = ? AND workspace_id = ? AND session_id IS NULL",
            (session_id, user_id, workspace_id),
        )
        await self._db.commit()

    async def list(self, user_id: str) -> list[dict]:
        """列出用户全部工作区（按创建时间），metadata 解析为 dict。"""
        async with self._db.execute(
            "SELECT workspace_id, name, metadata, volume_id, local_root FROM workspaces "
            "WHERE user_id = ? ORDER BY created_at, workspace_id",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "workspaceId": r[0],
                    "name": r[1],
                    "metadata": self._parse_metadata(r[2]),
                    "hasVolume": bool(r[3]),
                    "localRoot": r[4] or "",
                }
                for r in rows
            ]

    async def get(self, user_id: str, workspace_id: str) -> dict | None:
        """查询单个工作区（用于聊天页 workspaceId → 名称映射）。"""
        async with self._db.execute(
            "SELECT workspace_id, name, metadata, local_root, session_id FROM workspaces "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "workspaceId": row[0],
            "name": row[1],
            "metadata": self._parse_metadata(row[2]),
            "localRoot": row[3] or "",
            "sessionId": row[4] or "",
        }

    # ---- Volume 绑定 ----

    async def save_volume(
        self,
        user_id: str,
        workspace_id: str,
        volume_id: str,
        volume_name: str,
        volume_token: str,
    ) -> None:
        """绑定 Volume 到工作区（首次上传时懒创建）。"""
        await self._db.execute(
            "UPDATE workspaces SET volume_id = ?, volume_name = ?, volume_token = ? "
            "WHERE user_id = ? AND workspace_id = ?",
            (volume_id, volume_name, volume_token, user_id, workspace_id),
        )
        await self._db.commit()

    async def load_volume(
        self, user_id: str, workspace_id: str
    ) -> tuple[str, str, str] | None:
        """返回 (volume_id, volume_name, volume_token) 或 None（未创建 Volume）。"""
        async with self._db.execute(
            "SELECT volume_id, volume_name, volume_token FROM workspaces "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return row[0], row[1], row[2]

    async def clear_volume(self, user_id: str, workspace_id: str) -> None:
        """清空 Volume 绑定（销毁或连接失败后）。"""
        await self._db.execute(
            "UPDATE workspaces SET volume_id = NULL, volume_name = NULL, volume_token = NULL "
            "WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        )
        await self._db.commit()

    async def update_name(
        self, user_id: str, workspace_id: str, name: str
    ) -> bool:
        """重命名工作区（本次新增）。返回是否更新成功（不存在/归属不符返回 False）。"""
        async with self._db.execute(
            "UPDATE workspaces SET name = ? "
            "WHERE user_id = ? AND workspace_id = ?",
            (name, user_id, workspace_id),
        ) as cursor:
            updated = cursor.rowcount > 0
        await self._db.commit()
        return updated

    async def list_mounts(self, user_id: str) -> list[tuple[str, str]]:
        """列出用户已绑定 Volume 的工作区，供 Sandbox 挂载。

        返回 [(volume_name, workspace_id), ...]——E2B 挂载按 Volume 名称
        （volume_mounts 的 key 是挂载路径，value 是 Volume 名称/对象）。
        """
        async with self._db.execute(
            "SELECT volume_name, workspace_id FROM workspaces "
            "WHERE user_id = ? AND volume_name IS NOT NULL",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [(r[0], r[1]) for r in rows]

    # ---- 删除（级联 + 销毁 Volume 回调） ----

    async def delete(
        self,
        user_id: str,
        workspace_id: str,
        on_destroy_volume: object | None = None,
    ) -> bool:
        """删除工作区并级联删除其项目指令；有 Volume 时先调用销毁回调。

        on_destroy_volume: async callable (user_id, workspace_id, volume_id|None)
        -> None，由调用方注入（如 WorkspaceVolumeService.on_delete），解耦
        E2B 依赖便于测试。volume_id 可能为 None（Local 后端无 Volume 概念）。
        """
        record = await self.load_volume(user_id, workspace_id)
        if on_destroy_volume is not None:
            volume_id = record[0] if record is not None else None
            await on_destroy_volume(user_id, workspace_id, volume_id)  # type: ignore[misc]

        async with self._db.execute(
            "DELETE FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            deleted = cursor.rowcount > 0
        if deleted:
            await self._db.execute(
                "DELETE FROM project_memories WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            )
            # 保留关联会话：把该工作区下的消息 workspace_id 置 NULL（变为"普通对话"，
            # 出现在侧边栏「最近对话」），不丢用户历史——与 DeepSeek 等产品的行为一致。
            await self._db.execute(
                "UPDATE chat_messages SET workspace_id = NULL "
                "WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            )
        await self._db.commit()
        return deleted

    # ---- metadata 更新 ----

    async def update_files_metadata(
        self, user_id: str, workspace_id: str, file_paths: list[str]
    ) -> None:
        """把新上传的文件路径并入 metadata（camelCase，对齐前端 filePaths/fileCount）。"""
        async with self._db.execute(
            "SELECT metadata FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        metadata = json.loads(row[0]) if row and row[0] else {}

        existing = set(metadata.get("filePaths", []))
        added = [p for p in file_paths if p not in existing]
        if not added:
            return
        existing.update(added)
        metadata["filePaths"] = sorted(existing)
        metadata["fileCount"] = len(existing)

        await self._db.execute(
            "UPDATE workspaces SET metadata = ? WHERE user_id = ? AND workspace_id = ?",
            (json.dumps(metadata, ensure_ascii=False), user_id, workspace_id),
        )
        await self._db.commit()

    async def update_metadata(
        self, user_id: str, workspace_id: str, patch: dict
    ) -> dict | None:
        """合并 metadata 键（浅合并，patch 里的键覆盖旧值），返回更新后的 metadata。

        不存在/归属不符返回 None（调用方转 404）。
        """
        async with self._db.execute(
            "SELECT metadata FROM workspaces WHERE user_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        metadata = self._parse_metadata(row[0])
        metadata.update(patch)
        await self._db.execute(
            "UPDATE workspaces SET metadata = ? WHERE user_id = ? AND workspace_id = ?",
            (json.dumps(metadata, ensure_ascii=False), user_id, workspace_id),
        )
        await self._db.commit()
        return metadata

    @staticmethod
    def _parse_metadata(raw: str | None) -> dict:
        """metadata 列是 JSON 字符串，解析失败返回空 dict（容错）。"""
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
