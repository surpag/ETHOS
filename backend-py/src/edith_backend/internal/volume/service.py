"""volume 服务：把 Clerk 用户绑定到唯一的 E2B Volume。

对应 Go 版 internal/volume/service.go。
"""

from __future__ import annotations

import asyncio
import logging

from e2b import AsyncVolume

# from e2b import Sandbox
from .store import Store
from .trace import token_fingerprint, trace_enabled, user_fingerprint
from .types import CUSTOM_SKILLS_PATH, USER_OVERVIEW_PATH, Mount, UserVolume

logger = logging.getLogger(__name__)


class Service:
    """负责把 Clerk 用户绑定到唯一的 E2B Volume。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def read_user_overview(self, user_id: str) -> str:
        """读取已有用户 Volume 中的 Skills 摘要。

        overview.md 是可选的派生索引：Volume 不存在、文件不存在或 E2B 不可用时
        返回空字符串，让 Agent 继续运行。不会创建远端 Volume。
        """
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("volume requires a user ID")
        record = await self._store.load(user_id)
        if record is None:
            return ""
        volume_id, _name, token = record
        if trace_enabled():
            logger.info(
                "e2b volume overview user_id=%s volume_id=%s token_sha256=%s token_len=%d",
                user_fingerprint(user_id),
                volume_id,
                token_fingerprint(token)[:12],
                len(token),
            )
        try:
            volume = await AsyncVolume.connect(volume_id)
            data = await volume.read_file(USER_OVERVIEW_PATH)
            return (data or "").strip()
        except Exception as exc:  # noqa: BLE001 - E2B 不可用时降级为空
            logger.warning(
                "skip unavailable user skill overview user_id=%s volume_id=%s error=%v",
                user_fingerprint(user_id),
                volume_id,
                exc,
            )
            return ""

    async def mount_for_user(self, user_id: str) -> Mount | None:
        """返回当前用户的 Skills Volume 挂载信息；首次调用会创建远端 Volume。

        E2B 账号未启用 Volumes 时降级返回 None，让 Sandbox 仍可创建，
        只是用户自定义 Skills 功能不可用。
        """
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("volume requires a user ID")
        try:
            volume = await self._get_or_create(user_id)
        except Exception as exc:  # noqa: BLE001 - 403 volumes not enabled 等降级
            logger.warning(
                "volumes unavailable, sandbox will mount without volume user_id=%s error=%s",
                user_fingerprint(user_id),
                exc,
            )
            return None
        return Mount(name=volume.name, path=CUSTOM_SKILLS_PATH)

    async def open_for_user(self, user_id: str) -> AsyncVolume:
        """连接当前用户的 Volume。"""
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("volume requires a user ID")
        record = await self._store.load(user_id)
        if record is None:
            raise ValueError("user volume not found")
        return await AsyncVolume.connect(record[0])

    async def _get_or_create(self, user_id: str) -> UserVolume:
        # 单实例下串行保护首次创建，避免远端产生两个同用户 Volume
        async with self._lock:
            record = await self._store.load(user_id)
            if record is not None:
                return UserVolume(id=record[0], name=record[1])
            name = "edith-user-volume-" + user_id
            created = await AsyncVolume.create(name)
            if not created.volume_id or not created.name or not created.token:
                await created.destroy()
                raise ValueError("create user volume returned incomplete information")
            await self._store.save(
                user_id, created.volume_id, created.name, created.token
            )
            return UserVolume(id=created.volume_id, name=created.name)
