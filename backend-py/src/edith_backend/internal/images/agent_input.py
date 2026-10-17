"""images 的 Agent 输入：把图片 ID 转为模型可访问的短期 URL。

对应 Go 版 internal/images/agent_input.go。
"""

from __future__ import annotations

import logging

from .service import Service

logger = logging.getLogger(__name__)

REFERENCE_PREFIX = "edith-image://"


class AgentInput:
    def __init__(self, service: Service) -> None:
        self._service = service

    async def message_image_urls(
        self, user_id: str, session_id: str, image_ids: list[str]
    ) -> list[str]:
        """把已确认图片转换为本次模型调用可访问的短期 URL。

        COS 不可用时降级跳过图片（返回能转的 URL 或空列表），对话继续。
        """
        if not image_ids:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for image_id in image_ids:
            image_id = image_id.strip()
            if not image_id:
                raise ValueError("imageID is required")
            if image_id in seen:
                raise ValueError(f'image "{image_id}" was supplied more than once')
            seen.add(image_id)
            try:
                urls.append(
                    await self._service.open_for_session(user_id, session_id, image_id)
                )
            except Exception as exc:  # noqa: BLE001 - COS 不可用降级为无图
                logger.warning(
                    "image unavailable, skipping user=%s session=%s image=%s error=%s",
                    user_id, session_id, image_id, exc,
                )
        return urls


def reference(image_id: str) -> str:
    """返回写入会话历史的持久图片标记。"""
    return REFERENCE_PREFIX + image_id


def image_id_from_reference(value: str) -> tuple[str, bool]:
    """从持久图片标记取回图片 ID。"""
    image_id = value.removeprefix(REFERENCE_PREFIX)
    return image_id, value.startswith(REFERENCE_PREFIX) and bool(image_id)
