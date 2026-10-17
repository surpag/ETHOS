"""用户模型供应商密钥的读写能力。

对应 Go 版 internal/userconfig/providers.go。
"""

from __future__ import annotations

import aiosqlite

from .secret import deobfuscate, obfuscate
from .store import ensure_user
from .types import ProviderStatus


class Providers:
    """提供用户模型供应商密钥的服务端读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def list_statuses(self, user_id: str) -> list[ProviderStatus]:
        """返回可安全暴露给浏览器的密钥配置状态。"""
        await ensure_user(self._db, user_id)
        cursor = await self._db.execute(
            """
            SELECT provider_id, api_key <> '' AS has_key
            FROM user_providers WHERE user_id = ? ORDER BY provider_id
            """,
            (user_id.strip(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ProviderStatus(provider_id=row["provider_id"], has_api_key=bool(row["has_key"]))
            for row in rows
        ]

    async def save(
        self,
        user_id: str,
        credentials: list[tuple[str, str | None]],
    ) -> None:
        """保存提交的密钥；api_key 为 None 的供应商保持不变。

        credentials 为 (provider_id, api_key_or_none) 列表。
        """
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await ensure_user(self._db, user_id)
        for provider_id, api_key in credentials:
            provider_id = provider_id.strip()
            if not provider_id:
                raise ValueError("provider id is required")
            if api_key is None:
                continue
            await self._db.execute(
                """
                INSERT INTO user_providers (user_id, provider_id, api_key)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, provider_id) DO UPDATE SET api_key = excluded.api_key
                """,
                (user_id, provider_id, obfuscate(api_key.strip())),
            )
        await self._db.commit()

    async def load_api_key(self, user_id: str, provider_id: str) -> str:
        """为一次 Agent 运行读取一个供应商的密钥。"""
        cursor = await self._db.execute(
            "SELECT api_key FROM user_providers WHERE user_id = ? AND provider_id = ?",
            (user_id.strip(), provider_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise LookupError(f"provider key not found: {user_id}/{provider_id}")
        return deobfuscate(row["api_key"])

    async def try_load_api_key(self, user_id: str, provider_id: str) -> str:
        """读取密钥，缺失时返回空串（用于「可选凭据」的探测路径）。"""
        try:
            return await self.load_api_key(user_id, provider_id)
        except LookupError:
            return ""

    async def delete(self, user_id: str, provider_id: str) -> None:
        """删除一个供应商的密钥（自定义供应商被移除时调用；幂等）。"""
        await self._db.execute(
            "DELETE FROM user_providers WHERE user_id = ? AND provider_id = ?",
            (user_id.strip(), provider_id.strip()),
        )
        await self._db.commit()
