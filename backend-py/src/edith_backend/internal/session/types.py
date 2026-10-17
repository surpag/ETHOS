"""会话消息的类型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MessageRecord:
    """一条持久化的对话消息。"""

    id: int
    role: str  # "user" | "assistant"
    content: str
    reasoning: str = ""
    image_ids: list[str] = field(default_factory=list)
    # 本次新增：本轮的工具调用记录（历史 Timeline 展示用），元素形如
    # {"id","name","arguments","status","result"}
    tool_calls: list[dict] = field(default_factory=list)
    created_at: str = ""
    # 本次新增：生成这条 assistant 消息所花的时间（毫秒）。用户消息恒为 None
    duration_ms: int | None = None

    def to_openai_message(self) -> dict[str, str]:
        """转为 OpenAI 消息格式（用于拼装模型上下文）。"""
        return {"role": self.role, "content": self.content}
