"""流式解码器：维护块 ID 状态，把内容增量与工具调用转为中性事件。

对应 Go 版 internal/agentstream/decoder.go。
每个事件流必须使用独立实例。
"""

from __future__ import annotations

from .types import Event


class StreamDecoder:
    """保存一次 Agent 回复中的块顺序，输出渠道中性事件。"""

    def __init__(self, assistant_id: str) -> None:
        self.assistant_id = assistant_id
        self.next_block_id = 0
        self.last_type = ""
        self.last_id = ""
        self.started_tools: set[str] = set()

    def content_delta(self, block_type: str, event_type: str, delta: str) -> Event:
        """生成 reasoning.delta 或 message.delta 事件。

        同一类型的连续增量共享同一个 BlockID；类型切换时分配新块。
        """
        if self.last_type != block_type:
            self.next_block_id += 1
            self.last_type = block_type
            self.last_id = f"{self.assistant_id}_{block_type}_{self.next_block_id}"
        return Event(
            type=event_type,
            assistant_id=self.assistant_id,
            block_id=self.last_id,
            block_type=block_type,
            delta=delta,
        )

    def tool_started(self, tool_call_id: str, tool_name: str, arguments: str) -> Event:
        """生成 tool.started 事件，并结束当前内容块。"""
        self.started_tools.add(tool_call_id)
        self.end_content_block()
        return Event(
            type="tool.started",
            assistant_id=self.assistant_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_status="running",
        )

    def tool_finished(self, tool_call_id: str, tool_status: str, tool_result: str) -> Event:
        """生成 tool.finished 事件，并结束当前内容块。"""
        self.end_content_block()
        return Event(
            type="tool.finished",
            assistant_id=self.assistant_id,
            tool_call_id=tool_call_id,
            tool_status=tool_status,
            tool_result=tool_result,
        )

    def end_content_block(self) -> None:
        self.last_type = ""
        self.last_id = ""
