"""run 内显式压缩的行为约束。

这些断言守的不是「压缩能跑通」，而是**压缩不越界**：
- 指令永远留在 messages[0]（信任通道不能因为压缩而换位置）；
- 摘要是整段替换产生的一条新消息，被保留下来的消息逐字节不变；
- 压不动 / 失败时原样返回，绝不半途改一半。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from edith_backend.internal.agentrun.execution import (
    SUMMARY_TAG,
    _compact_in_memory,
    _tool_pairs_intact,
    is_summary_message,
)
from edith_backend.internal.models.catalog import ModelDefinition
from edith_backend.internal.trace import new_tracer


class _FakeCompletions:
    def __init__(self, content: str = "- 已完成 A\n- 待办 B", fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=None,
        )


class _CompletionsWithUsage(_FakeCompletions):
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="- 摘要"))],
            usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20, total_tokens=120
            ),
        )


class _FakeClient:
    def __init__(self, completions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _definition() -> ModelDefinition:
    return ModelDefinition(
        id="m1",
        provider_id="p1",
        name="fake",
        api_model="fake-model",
        base_url="http://127.0.0.1:1",
        context_window=128000,
    )


def _messages(turns: int) -> list[dict]:
    """指令 + turns 个 turn；每个 turn 是 user / assistant(tool_calls) / tool。"""
    out: list[dict] = [{"role": "system", "content": "你是 EDITH"}]
    for i in range(turns):
        out.append({"role": "user", "content": f"第 {i} 个请求 " + "u" * 80})
        out.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{i}",
                        "type": "function",
                        "function": {"name": "sandbox_read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        out.append({"role": "tool", "tool_call_id": f"call-{i}", "content": "r" * 80})
    out.append({"role": "user", "content": "最后一问"})
    return out


def _compact(messages, completions, budget=200):
    async def run():
        tracer = new_tracer(None, "req-inline", "u1", "s1")
        return await _compact_in_memory(
            _FakeClient(completions),
            _definition(),
            messages,
            budget,
            tracer,
            "req-inline",
        )

    return asyncio.run(run())


def test_inline_compact_keeps_instruction_and_recent_turns() -> None:
    messages = _messages(turns=20)
    before = json.dumps(messages, ensure_ascii=False, sort_keys=True)

    compacted, tokens, ok = _compact(messages, _FakeCompletions())

    assert ok
    # 指令必须在 0：压缩不能改变信任通道的位置
    assert compacted[0] == {"role": "system", "content": "你是 EDITH"}
    # 摘要紧随其后，且带标记（可被 is_summary_message 定位）
    assert is_summary_message(compacted[1])
    assert SUMMARY_TAG in compacted[1]["content"]
    assert "已完成 A" in compacted[1]["content"]
    # 明显变短
    assert len(compacted) < len(messages)
    # 末尾（当前用户消息）保留
    assert compacted[-1] == {"role": "user", "content": "最后一问"}
    # 原列表未被就地改写：压缩是「替换出一份新列表」，不是原地编辑
    assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == before
    # 用量未上报 → None（不伪造成 0）
    assert tokens is None


def test_inline_compact_preserves_recent_messages_verbatim() -> None:
    """保留下来的消息必须逐字节等于原文——这是「历史不可变」的另一半。"""
    messages = _messages(turns=12)
    compacted, _, ok = _compact(messages, _FakeCompletions())
    assert ok
    tail = compacted[2:]
    originals = messages[1:]
    idx = 0
    for message in tail:
        while idx < len(originals) and originals[idx] != message:
            idx += 1
        assert idx < len(originals), "保留消息在原文中找不到完全一致的副本"
        idx += 1


def test_inline_compact_returns_unchanged_when_nothing_to_fold() -> None:
    """turn 太少（全在最近窗口内）：压不动，原样返回且标记 False。"""
    messages = _messages(turns=1)
    compacted, tokens, ok = _compact(messages, _FakeCompletions(), budget=10**6)
    assert ok is False
    assert tokens is None
    assert compacted == messages


def test_inline_compact_failure_keeps_history_intact() -> None:
    """摘要调用失败：不返回半成品（否则上下文会缺一段而没人知道）。"""
    messages = _messages(turns=20)
    compacted, tokens, ok = _compact(messages, _FakeCompletions(fail=True), budget=200)
    assert ok is False
    assert tokens is None
    assert compacted == messages


def test_inline_compact_drops_oversized_source_from_oldest(monkeypatch) -> None:
    """来源超预算时从最早开始丢，并在 prompt 里显式说明——不静默丢信息。"""
    from edith_backend.internal.agentrun import execution

    # 把来源上限压到很小，模拟「中段体量远超摘要请求可承受范围」
    monkeypatch.setattr(execution, "INLINE_COMPACT_SOURCE_CHARS", 1200)
    completions = _FakeCompletions()
    messages = _messages(turns=20)
    _compact(messages, completions, budget=200)
    assert completions.calls, "未发起摘要调用"
    prompt = completions.calls[0]["messages"][1]["content"]
    assert "更早的" in prompt and "未纳入本次摘要" in prompt
    # 最后一条（最新的中段消息）必须被保留，说明是「从最早开始丢」
    assert "第 19 个请求" in prompt


def test_tool_pairs_intact_detects_orphan_result() -> None:
    """孤立的 tool 结果 = 非法消息序列，压缩结果必须自检。

    正常路径下切分点只落在 user 边界（tool 结果永远跟它所属的 assistant 在同
    turn），但这条约束是「靠切分规则保证」而不是「被验证过」的，所以要兜一层。
    """
    paired = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "type": "function", "function": {}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]
    assert _tool_pairs_intact(paired)
    # 把 assistant 切走只留 tool 结果 → 非法
    assert not _tool_pairs_intact(paired[2:])
    # 缺 tool_call_id 也非法
    assert not _tool_pairs_intact([{"role": "tool", "content": "r"}])


def test_inline_compact_propagates_usage() -> None:
    """压缩本身是模型调用，用量必须回到 run 汇总，否则成本优化不可见。"""
    messages = _messages(turns=20)
    _, tokens, ok = _compact(messages, _CompletionsWithUsage(), budget=200)
    assert ok
    assert tokens is not None
    assert tokens.total_tokens == 120
