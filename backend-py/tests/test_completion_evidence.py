"""完成证据（verify.completion）的回归测试。

这里测的不是「模型有没有说谎」——那要读文本，误报率高、且新话术一出现就失效。
测的是一个纯事实判定：本轮有没有发生过可能改变外部状态的动作。
"""

from __future__ import annotations

import asyncio

import aiosqlite

from edith_backend.internal.agentrun.execution import (
    EVIDENCE_EXTERNAL,
    EVIDENCE_METADATA,
    EVIDENCE_MUTATED,
    EVIDENCE_NONE,
    EVIDENCE_READ_ONLY,
    _record_completion_evidence,
    completion_evidence,
)
from edith_backend.internal.trace.schema import create_schema
from edith_backend.internal.trace.store import Store
from edith_backend.internal.trace.tracer import new_tracer


async def _enabled_tracer(request_id: str):
    """trace 只在 store 存在时才真正记步骤（store=None 时全部空转）。"""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, new_tracer(Store(db), request_id=request_id, user_id="u1", session_id="s1")


def _rec(name: str, status: str = "completed") -> dict[str, str]:
    return {"id": "x", "name": name, "arguments": "{}", "status": status, "result": ""}


def test_no_tool_calls_is_no_evidence() -> None:
    assert completion_evidence([]) == (EVIDENCE_NONE, 0, 0, 0)


def test_read_only_tools_are_not_evidence_of_change() -> None:
    """只读工具（读文件/列目录）不能算作「做了事」——这正是假完成的典型形态。"""
    level, ok, mutating, external = completion_evidence(
        [_rec("sandbox_read_file"), _rec("sandbox_list_files")]
    )
    assert (level, ok, mutating, external) == (EVIDENCE_READ_ONLY, 2, 0, 0)


def test_mutating_tool_counts_as_evidence() -> None:
    level, ok, mutating, _ = completion_evidence(
        [_rec("sandbox_list_files"), _rec("sandbox_write_file")]
    )
    assert (level, ok, mutating) == (EVIDENCE_MUTATED, 2, 1)


def test_failed_mutating_tool_is_not_evidence() -> None:
    """调用了但失败 ≠ 改了东西。这是「只看 tool_calls>0」覆盖不到的那一格。"""
    assert completion_evidence([_rec("sandbox_write_file", status="failed")])[0] == (
        EVIDENCE_NONE
    )


def test_mcp_tool_is_external_not_mutating() -> None:
    """MCP 工具能力未知：既不能声称它改了东西，也不能排除。"""
    level, _, mutating, external = completion_evidence([_rec("github__create_issue")])
    assert (level, mutating, external) == (EVIDENCE_EXTERNAL, 0, 1)


def test_plan_update_is_metadata_only_not_mutated() -> None:
    """只更新计划 ≠ 产生了变更。

    计划是模型自己维护的元数据，对用户的工作区/仓库没有任何影响。把它算成
    `mutated`，等于让「纯聊天 + 顺手写计划」也拿到最高证据等级——等级一旦
    人人都有，就不再是证据。
    """
    level, ok, mutating, external = completion_evidence([_rec("plan_update")])
    assert (level, ok, mutating, external) == (EVIDENCE_METADATA, 1, 0, 0)


def test_plan_update_does_not_outrank_real_write() -> None:
    """混在真实写操作里时，真实写仍然决定等级（不能被元工具稀释）。"""
    level, _, mutating, _ = completion_evidence(
        [_rec("plan_update"), _rec("sandbox_write_file")]
    )
    assert (level, mutating) == (EVIDENCE_MUTATED, 1)


def test_warning_and_trace_step_for_completion_without_evidence() -> None:
    """end_reason=completed 且零工具调用 → 必须留下 trace 步骤与告警。"""

    async def go() -> None:
        db, tracer = await _enabled_tracer("r1")
        try:
            level = await _record_completion_evidence(
                tracer, "r1", [], "stop", "completed"
            )
            assert level == EVIDENCE_NONE
            steps = [s for s in tracer.trace.steps if s.type == "verify"]
            assert len(steps) == 1
            assert steps[0].name == "completion"
            assert steps[0].metadata["evidence"] == EVIDENCE_NONE
            assert steps[0].metadata["finish_reason"] == "stop"
        finally:
            await db.close()

    asyncio.run(go())


def test_no_warning_when_run_did_not_complete() -> None:
    """截断/轮次耗尽不算「声称完成」，不该按假完成统计。"""

    async def go() -> None:
        db, tracer = await _enabled_tracer("r2")
        try:
            level = await _record_completion_evidence(
                tracer, "r2", [], "length", "output_truncated"
            )
            assert level == EVIDENCE_NONE
            steps = [s for s in tracer.trace.steps if s.type == "verify"]
            assert steps[0].metadata["end_reason"] == "output_truncated"
        finally:
            await db.close()

    asyncio.run(go())
