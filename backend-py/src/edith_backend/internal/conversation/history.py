"""会话历史读取与 Timeline 投影。

对应 Go 版 internal/conversation/history.go + timeline_projection.go。
"""

from __future__ import annotations

from ..agentstream.outcome import derive_outcome
from ..agentstream.types import PlanSnapshot
from ..session.plan_store import RunPlanStore
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
    def __init__(
        self, store: SessionStore, usage: Reader, plans: RunPlanStore | None = None
    ) -> None:
        self.store = store
        self.usage = usage
        # 计划快照（可选依赖）：没有它时历史里不带计划，其余能力不受影响
        self.plans = plans

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
        plan = None
        if self.plans is not None:
            latest = await self.plans.latest(user_id.strip(), session_id.strip())
            if latest is not None:
                plan = PlanSnapshot(**latest)
        # 任务状态是「还有没有没跑完的活」的权威来源：前端刷新后据此提示续跑，
        # 不再依赖 sessionStorage 里那份只存在于当前标签页的 pending 记录。
        task_status = await self.store.get_task_status(user_id, session_id)
        return ConversationResponse(
            timeline=_timeline(records),
            usage=usage_summary,
            plan=plan,
            task_status=task_status,
        )


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
                    # id 必须和 SSE 的 `assistantId` 同源：它由 service 用
                    # `assistant_{request_id}` 生成。两边对不上时，刷新后「历史里
                    # 那条」与「重连补发出来的那条」会被当成两个块，屏幕上就出现
                    # 两份回复。旧行没有 request_id，退回按自增 id 编号。
                    id=(
                        f"assistant_{record.request_id}"
                        if record.request_id
                        else f"assistant_{record.id}"
                    ),
                    created_at=record.created_at,
                    blocks=content_blocks,
                    duration_ms=record.duration_ms,
                    completion_evidence=record.completion_evidence,
                    # 服务端派生（唯一入口在 agentstream.outcome）：UI 拿到的是
                    # 语义，不是原因，因此后端新增终止原因不需要改前端。
                    outcome=derive_outcome(record.end_reason),
                    end_reason=record.end_reason,
                    # 计划是 run 级状态：把 run 的标识带上，前端才能把计划面板
                    # 钉在「产生它的那一轮」下面，而不是画在时间线末尾。
                    request_id=record.request_id,
                )
            )
    return Timeline(blocks=blocks)
