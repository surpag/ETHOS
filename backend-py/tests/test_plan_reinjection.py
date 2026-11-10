"""计划的「状态重放」：不允许因为压缩/续跑而丢失。

计划是**当前状态**，不是**发生过的事**。历史可以被压缩成摘要，状态不行——
一旦丢了，模型会重新规划、重复已完成的工作，或者把「已经做过」记成「还没做」。
所以这里守两条：
1. 渲染出来的计划块是纯文本、带状态记号、未核实的 done 必须显式标出；
2. 压缩时计划块挂在摘要里，且**永远只有一份**（多次压缩不能堆出互相矛盾的旧计划）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from edith_backend.internal.agentrun.execution import (
    PLAN_BLOCK_HEADER,
    _attach_plan_block,
    _compact_in_memory,
    _strip_plan_block,
    is_summary_message,
)
from edith_backend.internal.agentrun.run_plan import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    PlanItem,
    render_plan_block,
)
from edith_backend.internal.models.catalog import ModelDefinition
from edith_backend.internal.trace import new_tracer


def _items() -> list[PlanItem]:
    return [
        PlanItem(id="1", title="读取 HTML 文件", status=STATUS_DONE, verified=True),
        PlanItem(id="2", title="修复参考图加载", status=STATUS_DONE, verified=False),
        PlanItem(id="3", title="验证修复结果", status=STATUS_IN_PROGRESS),
        PlanItem(id="4", title="输出交付文件", status=STATUS_PENDING),
        PlanItem(id="5", title="清理临时文件", status=STATUS_BLOCKED),
    ]


def test_render_plan_block_has_marks_and_summary() -> None:
    text = render_plan_block(_items())
    assert text.startswith("【当前计划】（已完成 2 / 进行中 1 / 待处理 1 / 受阻 1）")
    assert "[x] 读取 HTML 文件" in text
    assert "[~] 验证修复结果" in text
    assert "[ ] 输出交付文件" in text
    assert "[!] 清理临时文件" in text


def test_render_plan_block_marks_unverified_done() -> None:
    """未核实的 done 和已核实的 done 看起来一样，必须显式区分。

    一个是「有调用能证明」，一个是「模型自己写的」——把两者显示成同一个 [x]，
    等于替模型背书。
    """
    text = render_plan_block(_items())
    assert "[x] 修复参考图加载（未核实）" in text
    assert "读取 HTML 文件（未核实）" not in text


def test_render_plan_block_empty_when_no_items() -> None:
    assert render_plan_block([]) == ""
    assert render_plan_block([{"id": "1", "title": ""}]) == ""


def test_render_plan_block_accepts_store_dicts() -> None:
    """续跑时读到的是库里的数据库行（dict），不是 PlanItem 对象，两种都要能吃。"""
    text = render_plan_block(
        [{"id": "1", "title": "迁移数据库", "status": STATUS_DONE, "verified": True}]
    )
    assert "[x] 迁移数据库" in text


def test_plan_block_strips_back_to_original_summary() -> None:
    """再次压缩时，摘要里的计划块必须能被摘掉。

    否则旧计划会被当成「对话内容」喂给摘要模型，模型会把它复述成历史事实，
    然后新的摘要里就多了一段凭空生成的计划叙事。
    """
    summary = "用户要求修复画布；已完成 2 项。"
    block = render_plan_block(_items(), header=PLAN_BLOCK_HEADER)
    merged = _attach_plan_block(summary, block)
    assert merged.startswith(summary)
    assert PLAN_BLOCK_HEADER in merged
    assert _strip_plan_block(merged) == summary


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="- 摘要"))],
            usage=None,
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
    out: list[dict] = [{"role": "system", "content": "你是 EDITH"}]
    for i in range(turns):
        out.append({"role": "user", "content": f"第 {i} 个请求 " + "u" * 80})
        out.append({"role": "assistant", "content": "ok" + "a" * 80})
    out.append({"role": "user", "content": "最后一问"})
    return out


def _compact(messages, plan_block: str):
    async def run():
        tracer = new_tracer(None, "req-plan", "u1", "s1")
        return await _compact_in_memory(
            _FakeClient(_FakeCompletions()),
            _definition(),
            messages,
            200,
            tracer,
            "req-plan",
            plan_block=plan_block,
        )

    return asyncio.run(run())


def test_compact_keeps_plan_state_in_summary() -> None:
    """压缩可以丢掉任意多轮对话，但不能丢掉「做到第几步」。"""
    block = render_plan_block(_items(), header=PLAN_BLOCK_HEADER)
    compacted, _, ok = _compact(_messages(turns=20), block)
    assert ok
    summary_message = compacted[1]
    assert is_summary_message(summary_message)
    assert PLAN_BLOCK_HEADER in summary_message["content"]
    assert "验证修复结果" in summary_message["content"]
    # 计划只有一份：不能既在摘要里、又作为独立 system 消息再出现一次
    assert sum(
        1 for message in compacted if PLAN_BLOCK_HEADER in (message.get("content") or "")
    ) == 1


def test_second_compaction_does_not_duplicate_plan_block() -> None:
    """连续压缩两次，计划块仍然只有一份（新计划替换旧计划，而不是叠加）。"""
    first_block = render_plan_block(_items(), header=PLAN_BLOCK_HEADER)
    compacted, _, ok = _compact(_messages(turns=20), first_block)
    assert ok

    updated = [PlanItem(id="3", title="验证修复结果", status=STATUS_DONE, verified=True)]
    second_block = render_plan_block(updated, header=PLAN_BLOCK_HEADER)
    # 再喂一批新对话进去，模拟第二次压缩
    again, _, ok2 = _compact([*compacted, *_messages(turns=20)[1:]], second_block)
    assert ok2
    text = again[1]["content"]
    assert text.count(PLAN_BLOCK_HEADER) == 1
    assert "输出交付文件" not in text  # 旧计划的项不该被当成摘要内容带过来
