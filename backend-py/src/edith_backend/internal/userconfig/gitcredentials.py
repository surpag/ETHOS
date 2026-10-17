"""用户 Git 凭据（PAT）的读写能力。

定位：凭据**只在后端使用**（注入 git 执行环境跑 push/pull），永不下沉到 Agent 可达的环境。
存储复用 `secret.obfuscate`，与 user_providers 保持一致（单一权威源）。

⚠️ 上云前必须解决（见 secret.py 自述）：obfuscate 是基础混淆而非强加密，
其"真正的边界是本机单用户"。云端多租户部署前必须换成 KMS / 信封加密。
"""

from __future__ import annotations

import aiosqlite

from .secret import deobfuscate, obfuscate
from .store import ensure_user

# 先只支持 GitHub（凭据语义清晰：fine-grained PAT 或 classic PAT）
SUPPORTED_PROVIDERS = frozenset({"github"})


class GitCredentials:
    """用户 Git 凭据的服务端读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def list_connected(self, user_id: str) -> list[str]:
        """返回已配置凭据的 provider（供前端显示「已连接」，不暴露 token）。"""
        await ensure_user(self._db, user_id)
        cursor = await self._db.execute(
            "SELECT provider FROM user_git_credentials WHERE user_id = ? ORDER BY provider",
            (user_id.strip(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["provider"] for row in rows]

    async def save(self, user_id: str, provider: str, token: str) -> None:
        """保存（或覆盖）一个 provider 的 token。"""
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        provider = provider.strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported git provider: {provider}")
        token = token.strip()
        if not token:
            raise ValueError("token is required")
        await ensure_user(self._db, user_id)
        await self._db.execute(
            """
            INSERT INTO user_git_credentials (user_id, provider, token)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                token = excluded.token, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, provider, obfuscate(token)),
        )
        await self._db.commit()

    async def delete(self, user_id: str, provider: str) -> bool:
        """删除一个 provider 的凭据；返回是否确实删除了记录。"""
        cursor = await self._db.execute(
            "DELETE FROM user_git_credentials WHERE user_id = ? AND provider = ?",
            (user_id.strip(), provider.strip().lower()),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def load_token(self, user_id: str, provider: str = "github") -> str | None:
        """为一次 git 操作读取 token（仅后端调用）；未配置返回 None。"""
        cursor = await self._db.execute(
            "SELECT token FROM user_git_credentials WHERE user_id = ? AND provider = ?",
            (user_id.strip(), provider.strip().lower()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return deobfuscate(row["token"]) if row else None
