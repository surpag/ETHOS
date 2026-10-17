"""会话历史读取与 Timeline 投影。

对应 Go 版 internal/conversation/history.go + timeline_projection.go。
"""

from __future__ import annotations

from ..session.store import SessionStore
from ..session.types import MessageRecord
from ..usage.reader import Reader
from .types import (
    AssistantBlock,
    AssistantContentBlock,
    Conversation,
    ConversationResponse,
    Timeline,
    UserBlock,
    UserImage,
)

_TITLE_LIMIT = 18


class ConversationNotFound(Exception):
    pass


class History:
    def __init__(self, store: SessionStore, usage: Reader) -> None:
        self.store = store
        self.usage = usage

    async def list(self, user_id: str, include_archived: bool = False) -> list[Conversation]:
        """返回用户全部会话的侧边栏摘要（默认排除已归档）。"""
        sessions = await self.store.list_sessions(user_id.strip(), include_archived)
        return [
            Conversation(
                id=session_id,
                title=_title(first_message),
                updated_at=updated_at,
                workspace_id=workspace_id or "",
                archived=archived,
            )
            for session_id, first_message, updated_at, workspace_id, archived in sessions
        ]

    async def archive(self, user_id: str, session_id: str) -> None:
        """本次新增：归档会话。"""
        await self.store.archive(user_id.strip(), session_id.strip())

    async def unarchive(self, user_id: str, session_id: str) -> None:
        """本次新增：取消归档。"""
        await self.store.unarchive(user_id.strip(), session_id.strip())

    async def delete(self, user_id: str, session_id: str) -> bool:
        """本次新增：删除整个会话（消息 + 摘要 + 归档状态）。"""
        return await self.store.delete_session(user_id.strip(), session_id.strip())

    async def active_in_workspace(self, user_id: str, workspace_id: str) -> str:
        """该工作区当前的活跃会话 ID（没有则空串）。

        前端在「进入工作区 / 点新会话」时先问这里：一个工作区只允许一个会话，
        有了就直接打开它，而不是造出第二个沙箱去覆盖同一份目录。
        """
        return await self.store.find_active_session(
            user_id.strip(), workspace_id.strip()
        )

    async def get(self, user_id: str, session_id: str) -> ConversationResponse:
        """返回一个会话的 Timeline 和服务器计算用量。"""
        records = await self.store.get_records(user_id.strip(), session_id.strip())
        if not records:
            raise ConversationNotFound("conversation not found")
        usage_summary = await self.usage.session_summary(user_id, session_id)
        return ConversationResponse(timeline=_timeline(records), usage=usage_summary)


def _title(first_message: str) -> str:
    content = first_message.strip()
    if not content:
        return "新对话"
    if len(content) <= _TITLE_LIMIT:
        return content
    return content[:_TITLE_LIMIT]


def _timeline(records: list[MessageRecord]) -> Timeline:
    blocks = []
    for record in records:
        if record.role == "user":
            blocks.append(
                UserBlock(
                    id=f"user_{record.id}",
                    content=record.content,
                    images=[UserImage(id=i) for i in record.image_ids],
                    created_at=record.created_at,
                )
            )
        else:
            content_blocks: list[AssistantContentBlock] = []
            if record.reasoning:
                content_blocks.append(
                    AssistantContentBlock(
                        type="reasoning",
                        id=f"assistant_{record.id}_reasoning",
                        content=record.reasoning,
                    )
                )
            # 本次新增：工具调用块（按执行顺序），让刷新后历史里也能看到 Agent 调用了哪些工具
            for tc in record.tool_calls:
                content_blocks.append(
                    AssistantContentBlock(
                        type="tool",
                        id=f"assistant_{record.id}_tool_{tc.get('id', '')}",
                        tool_name=tc.get("name", ""),
                        arguments=tc.get("arguments", ""),
                        status=tc.get("status", ""),
                        result=tc.get("result", ""),
                    )
                )
            if record.content:
                content_blocks.append(
                    AssistantContentBlock(
                        type="text",
                        id=f"assistant_{record.id}_text",
                        content=record.content,
                    )
                )
            blocks.append(
                AssistantBlock(
                    id=f"assistant_{record.id}",
                    created_at=record.created_at,
                    blocks=content_blocks,
                    duration_ms=record.duration_ms,
                )
            )
    return Timeline(blocks=blocks)
