"""conversation 的类型定义。

对应 Go 版 internal/conversation/types.go。
Timeline 的块用 Pydantic 联合类型表达多态。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ..usage.types import Summary


class Conversation(BaseModel):
    """侧边栏展示的一段会话摘要。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    updated_at: str
    workspace_id: str = ""  # 空 = 普通对话（不属于任何工作区）
    archived: bool = False  # 本次新增：是否已归档


class ConversationListResponse(BaseModel):
    """会话列表 HTTP 响应。"""

    conversations: list[Conversation]


class WorkspaceSessionResponse(BaseModel):
    """某个工作区当前的活跃会话。

    sessionId 为空表示该工作区还没有会话（可以新建）。非空时前端必须直接打开它 ——
    一个工作区只允许一个会话，否则两个沙箱会互相覆盖同一份目录镜像。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    session_id: str = ""


class ConversationResponse(BaseModel):
    """一个会话的 Timeline 和用量 HTTP 响应。"""

    timeline: Timeline
    usage: Summary


class UserImage(BaseModel):
    """用户消息中引用的 EDITH 图片。"""

    id: str


class UserBlock(BaseModel):
    """一条用户消息卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "user"
    id: str
    content: str
    images: list[UserImage] = Field(default_factory=list)
    created_at: str


class AssistantContentBlock(BaseModel):
    """reasoning、text 或 tool 的展示块。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    id: str
    content: str = ""
    tool_name: str = ""
    arguments: str = ""
    status: str = ""
    result: str = ""


class AssistantBlock(BaseModel):
    """一整段 AI 回复卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "assistant"
    id: str
    created_at: str
    blocks: list[AssistantContentBlock] = Field(default_factory=list)
    # 本轮耗时（毫秒）；旧数据或未记录时为 None
    duration_ms: int | None = None


class ErrorBlock(BaseModel):
    """一次历史运行错误卡片。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = "error"
    id: str
    message: str
    created_at: str


class Timeline(BaseModel):
    """浏览器按时间顺序展示的一段对话。"""

    blocks: list[UserBlock | AssistantBlock | ErrorBlock] = Field(default_factory=list)
