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
    # 完成证据等级（mutated/external/read_only/none）。旧行与用户消息为 None，
    # 表示「未知」，不能当成 none（那会凭空指控历史消息没有工具调用）。
    completion_evidence: str | None = None
    # 终止原因（诊断用，不参与任何判定）。None = 该列引入前的旧行，或用户取消
    # （取消不落 assistant 消息）。语义投影见 agentstream.outcome.derive_outcome。
    end_reason: str | None = None
    # 产生这条回复的 run。计划（run_plans）按 request_id 归档，前端靠它把计划
    # 定位到「产生它的那一轮」，而不是画在时间线末尾（那会看起来属于最新消息）。
    # None = 该列引入前的旧行。
    request_id: str | None = None

    def to_openai_message(self) -> dict[str, str]:
        """转为 OpenAI 消息格式（用于拼装模型上下文）。"""
        return {"role": self.role, "content": self.content}
