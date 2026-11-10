"""E2B Volume 实现：权威副本在卷上，沙箱挂载即共享。

保留这个后端有两个理由：
1. E2B 一旦开通 Volumes，改一个环境变量即可切回（不必改 Agent 逻辑）；
2. 卷挂载没有「同步」这一步，天然不存在双副本漂移。

它只是把既有的 `internal/volume/service.py` 包一层抽象，**不重写**那里的
创建/连接/降级逻辑（那份实现已经覆盖懒创建、403 降级、token 指纹日志）。
"""

from __future__ import annotations

import logging

from ...volume.service import Service as VolumeService
from ...volume.types import USER_OVERVIEW_PATH
from .base import (
    OVERVIEW_FILENAME,
    SANDBOX_SKILLS_PATH,
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    MountSpec,
    SkillIndex,
    SkillStorageBackend,
)

logger = logging.getLogger(__name__)


class E2BSkillBackend(SkillStorageBackend):
    """基于 E2B Volume 的用户 Skill 存储。"""

    name = "e2b"

    def __init__(self, volumes: VolumeService) -> None:
        self._volumes = volumes

    async def mount_spec(self, user_id: str) -> MountSpec | None:
        mount = await self._volumes.mount_for_user(user_id)
        if mount is None:
            # 403 / 未开通：沙箱照建，只是不含用户 Skill。这不是静默——调用方
            # 会把「本用户没有挂载」与「有挂载但为空」区分开记录。
            return None
        return MountSpec(name=mount.name, path=mount.path or SANDBOX_SKILLS_PATH)

    async def read_index(self, user_id: str) -> SkillIndex:
        try:
            volume = await self._volumes.open_for_user(user_id)
        except ValueError:
            # 卷还没创建过 = 该用户没有任何自定义 Skill（不是故障）
            return SkillIndex(status=STATUS_EMPTY, reason="user volume not created")
        except Exception as exc:  # noqa: BLE001 - 连接失败是故障，必须区别于空
            return SkillIndex(
                status=STATUS_UNAVAILABLE, reason=f"connect volume failed: {exc}"
            )

        try:
            exists = await volume.exists(USER_OVERVIEW_PATH)
        except Exception as exc:  # noqa: BLE001
            return SkillIndex(
                status=STATUS_UNAVAILABLE, reason=f"probe overview failed: {exc}"
            )
        if not exists:
            return SkillIndex(status=STATUS_EMPTY, reason=f"{OVERVIEW_FILENAME} not found")

        try:
            data = await volume.read_file(USER_OVERVIEW_PATH)
        except Exception as exc:  # noqa: BLE001
            return SkillIndex(
                status=STATUS_UNAVAILABLE, reason=f"read overview failed: {exc}"
            )
        text = (data or "").strip()
        return SkillIndex(text=text, status=STATUS_OK if text else STATUS_EMPTY)
