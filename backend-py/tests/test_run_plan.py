"""计划状态机与证据校验的回归测试。

这里锁的是 A3 的核心承诺：**计划项标 done 必须能指向本轮真实的工具调用**。
如果这条不成立，计划就只是「更可信的假象」——一个打勾的列表比一段话更容易让人信。
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest

from edith_backend.internal.agentrun.run_plan import (
    MAX_PLAN_ITEMS,
    MODE_SET,
    MODE_UPDATE,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    RunPlan,
    RunPlanRegistry,
    resolve_evidence,
)
from edith_backend.internal.conversation.history import History
from edith_backend.internal.session.plan_store import RunPlanStore
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.usage.types import Summary


def _fact(
    call_id: str,
    name: str = "sandbox_write_file",
    arguments: str = '{"path":"src/a.py"}',
    status: str = "completed",
    result: str = '{"ok":true}',
) -> dict:
    return {
        "id": call_id,
        "name": name,
        "arguments": arguments,
        "status": status,
        "result": result,
    }


def _plan(facts: list[dict] | None = None) -> RunPlan:
    return RunPlan(
        request_id="r1",
        user_id="u1",
        session_id="s1",
        facts=facts if facts is not None else [],
    )


# ── 证据解析：只认事实，且由服务端自己找记录 ──────────────────────────


def test_path_evidence_resolves_to_the_matching_call() -> None:
    """模型只报路径，服务端自己找到对应调用——这是模型唯一做得到的事。"""
    facts = [
        _fact("c1", arguments='{"path":"workspace/w/src/a.py"}'),
        _fact("c2", arguments='{"path":"workspace/w/src/b.py"}'),
    ]
    resolved, failures = resolve_evidence({"path": "src/a.py"}, facts)
    assert failures == []
    assert resolved == {"tool_call_ids": ["c1"], "path": "src/a.py"}


def test_relative_and_absolute_paths_are_normalized() -> None:
    facts = [_fact("c1", arguments='{"path":"workspace/w/src/a.py"}')]
    for claim in ("./src/a.py", "src\\a.py", "/src/a.py", "src/a.py"):
        resolved, failures = resolve_evidence({"path": claim}, facts)
        assert failures == [], claim
        assert resolved["tool_call_ids"] == ["c1"]


def test_read_only_call_cannot_prove_a_file_change() -> None:
    facts = [_fact("c1", name="sandbox_read_file", arguments='{"path":"src/a.py"}')]
    _, failures = resolve_evidence({"path": "src/a.py"}, facts)
    assert any("只读" in reason for reason in failures)


def test_business_error_recorded_as_completed_is_not_evidence() -> None:
    """既有坑：业务错误（retryable=True）在记录里是 completed。

    只看 status 会把失败当成功——这正是「假完成」的原产地，所以必须看 result.error。
    """
    facts = [
        _fact(
            "c1",
            arguments='{"path":"src/a.py"}',
            result='{"error":"目录不存在","kind":"command_error","retryable":true}',
        )
    ]
    _, failures = resolve_evidence({"path": "src/a.py"}, facts)
    assert any("改动过 src/a.py" in reason for reason in failures)


def test_exit_code_evidence_resolves_command() -> None:
    facts = [
        _fact(
            "c1",
            name="sandbox_run_command",
            arguments='{"command":"mvn test"}',
            result='{"exit_code":0}',
        )
    ]
    resolved, failures = resolve_evidence({"expect_exit_code": 0}, facts)
    assert failures == []
    assert resolved["tool_call_ids"] == ["c1"]


def test_exit_code_reads_camel_case_from_real_tool_result() -> None:
    """sandbox_run_command 真实返回是 {"exitCode": 0}。

    第一版读的是 exit_code，导致所有命令类证据静默失效（全部返回 None）——
    真实 run 里模型两次被误拒才发现。这条用例锁定真实字段名。
    """
    facts = [
        _fact(
            "c1",
            name="sandbox_run_command",
            arguments='{"command":"cat second.txt"}',
            result='{"exitCode": 0, "stdout": "hello again"}',
        )
    ]
    resolved, failures = resolve_evidence({"expect_exit_code": 0}, facts)
    assert failures == []
    assert resolved["tool_call_ids"] == ["c1"]


def test_nonzero_exit_command_is_not_a_successful_call() -> None:
    """非零退出不带 error 字段（用 kind=command_error），必须靠退出码判定。"""
    facts = [
        _fact(
            "c1",
            name="sandbox_run_command",
            arguments='{"command":"cat missing.txt"}',
            result='{"exitCode": 2, "stderr": "No such file", "kind": "command_error", "retryable": true}',
        )
    ]
    _, failures = resolve_evidence({"path": "missing.txt"}, facts)
    assert any("没有找到" in reason for reason in failures)


def test_exit_code_mismatch_lists_actual_codes() -> None:
    facts = [
        _fact(
            "c1",
            name="sandbox_run_command",
            arguments='{"command":"mvn test"}',
            result='{"exit_code":1}',
        )
    ]
    _, failures = resolve_evidence({"expect_exit_code": 0}, facts)
    assert any("退出码为 0" in reason and "[1]" in reason for reason in failures)


def test_explicit_call_ids_are_still_honoured() -> None:
    """可选字段：给了就必须对得上（防止模型照抄一个不存在的 id 蒙混）。"""
    facts = [_fact("c1", arguments='{"path":"src/a.py"}')]
    _, failures = resolve_evidence(
        {"tool_call_ids": ["ghost"], "path": "src/a.py"}, facts
    )
    assert any("ghost" in reason for reason in failures)


def test_no_facts_at_all_is_rejected() -> None:
    _, failures = resolve_evidence({"path": "src/a.py"}, [])
    assert any("还没有任何已完成的工具调用" in reason for reason in failures)


def test_vague_evidence_is_rejected() -> None:
    _, failures = resolve_evidence({"note": "done"}, [_fact("c1")])
    assert any("太空泛" in reason for reason in failures)


def test_rejection_lists_available_calls_for_self_correction() -> None:
    """拒绝信息必须回到模型能理解的语言（它知道自己写过哪个路径）。"""
    facts = [_fact("c1", arguments='{"path":"src/other.py"}')]
    _, failures = resolve_evidence({"path": "src/a.py"}, facts)
    assert any("sandbox_write_file" in reason and "src/other.py" in reason for reason in failures)


# ── 状态机 ────────────────────────────────────────────────────────────


def test_set_plan_with_done_item_but_no_evidence_is_rejected() -> None:
    plan = _plan()
    ok, failures, _ = plan.apply(
        MODE_SET,
        [
            {"id": "1", "title": "读规格", "status": STATUS_DONE},
            {"id": "2", "title": "实现", "status": STATUS_IN_PROGRESS},
            {"id": "3", "title": "跑测试", "status": "pending"},
        ],
    )
    # 第 1 项标了 done 却没给证据 → 整批拒绝，计划保持为空
    assert ok is False
    assert any("没有 evidence" in reason for reason in failures)
    assert plan.items == [] and plan.revision == 0


def test_set_plan_without_done_succeeds() -> None:
    plan = _plan()
    ok, failures, summary = plan.apply(
        MODE_SET,
        [
            {"id": "1", "title": "读规格", "status": STATUS_IN_PROGRESS},
            {"id": "2", "title": "实现"},
        ],
    )
    assert (ok, failures) == (True, [])
    assert summary["total"] == 2 and summary["in_progress"] == 1
    assert summary["pending"] == 1
    assert plan.revision == 1


def test_done_requires_evidence_and_verifies() -> None:
    facts = [_fact("c1")]
    plan = _plan(facts)
    rejected, reasons, _ = plan.apply(
        MODE_SET, [{"id": "1", "title": "写文件", "status": STATUS_DONE}]
    )
    assert rejected is False and reasons

    ok, failures, summary = plan.apply(
        MODE_SET,
        [
            {
                "id": "1",
                "title": "写文件",
                "status": STATUS_DONE,
                "evidence": {"tool_call_ids": ["c1"], "path": "src/a.py"},
            }
        ],
    )
    assert (ok, failures) == (True, [])
    assert summary["done"] == 1
    assert plan.items[0].verified is True


def test_invalid_evidence_keeps_previous_plan_untouched() -> None:
    """整批拒绝、不部分生效：否则计划与现实错位，比拒绝更难排查。"""
    plan = _plan([])
    plan.apply(
        MODE_SET,
        [{"id": "1", "title": "实现", "status": STATUS_IN_PROGRESS}],
    )
    before = [item.to_dict() for item in plan.items]
    revision_before = plan.revision

    ok, reasons, _ = plan.apply(
        MODE_UPDATE,
        [
            {"id": "1", "status": STATUS_DONE, "evidence": {"tool_call_ids": ["ghost"]}}
        ],
    )
    assert ok is False and reasons
    assert [item.to_dict() for item in plan.items] == before
    assert plan.revision == revision_before


def test_update_unknown_id_is_rejected() -> None:
    plan = _plan()
    plan.apply(MODE_SET, [{"id": "1", "title": "实现"}])
    ok, reasons, _ = plan.apply(MODE_UPDATE, [{"id": "9", "status": STATUS_BLOCKED}])
    assert ok is False
    assert any("未知计划项 id" in r for r in reasons)


def test_second_done_without_new_evidence_keeps_verification() -> None:
    """已核验的 done 项重复提交时不要求重新给证据（否则每轮都得重发全文）。"""
    facts = [_fact("c1")]
    plan = _plan(facts)
    evidence = {"tool_call_ids": ["c1"], "path": "src/a.py"}
    plan.apply(
        MODE_SET,
        [{"id": "1", "title": "写文件", "status": STATUS_DONE, "evidence": evidence}],
    )
    ok, failures, _ = plan.apply(
        MODE_UPDATE, [{"id": "1", "status": STATUS_DONE}]
    )
    assert (ok, failures) == (True, [])
    assert plan.items[0].verified is True


@pytest.mark.parametrize(
    "items,fragment",
    [
        ([], "不能为空"),
        ([{"id": str(i), "title": "x"} for i in range(MAX_PLAN_ITEMS + 1)], "最多"),
        ([{"id": "1", "title": ""}], "缺少 title"),
        ([{"id": "1", "title": "x", "status": "doing"}], "status 非法"),
        ([{"id": "1", "title": "x"}, {"id": "1", "title": "y"}], "id 重复"),
    ],
)
def test_set_plan_rejects_bad_input(items: list, fragment: str) -> None:
    plan = _plan()
    ok, reasons, _ = plan.apply(MODE_SET, items)
    assert ok is False
    assert any(fragment in reason for reason in reasons)


def test_unknown_mode_is_rejected() -> None:
    ok, reasons, _ = _plan().apply("append", [{"id": "1", "title": "x"}])
    assert ok is False and any("未知 mode" in r for r in reasons)


# ── 注册表 ────────────────────────────────────────────────────────────


def test_registry_open_current_close() -> None:
    registry = RunPlanRegistry()
    facts: list[dict] = []
    plan = registry.open(
        user_id="u1", session_id="s1", request_id="r1", facts=facts
    )
    assert registry.current("u1", "s1") is plan
    assert plan.facts is facts  # 必须是同一对象：工具执行期间 records 会被追加
    registry.close("r1")
    assert registry.current("u1", "s1") is None


def test_registry_replaces_leaked_entry() -> None:
    """上一次 run 的 close 没执行到时，新 run 必须能覆盖，不能被永久挡住。"""
    registry = RunPlanRegistry()
    registry.open(user_id="u1", session_id="s1", request_id="r1", facts=[])
    plan = registry.open(user_id="u1", session_id="s1", request_id="r2", facts=[])
    assert registry.current("u1", "s1") is plan
    registry.close("r2")
    assert registry.current("u1", "s1") is None


# ── 持久化 ────────────────────────────────────────────────────────────


def test_plan_store_roundtrip_keeps_latest_revision() -> None:
    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        try:
            store = RunPlanStore(db)
            assert await store.latest("u1", "s1") is None
            await store.save("u1", "s1", "r1", 1, [{"id": "1", "title": "a", "status": "pending"}])
            await store.save("u1", "s1", "r1", 2, [{"id": "1", "title": "a", "status": "done"}])
            latest = await store.latest("u1", "s1")
            assert latest is not None
            assert latest["revision"] == 2
            assert latest["items"][0]["status"] == "done"
            # 删会话要带走计划快照
            await store.delete_for_session("u1", "s1")
            assert await store.latest("u1", "s1") is None
        finally:
            await db.close()

    import asyncio

    asyncio.run(go())


# ── 历史投影（「三处同源」的第三处） ──────────────────────────────────


def test_history_projects_persisted_plan() -> None:
    """刷新后计划仍在：历史接口把最新快照投影成 ConversationResponse.plan。

    这条路径此前没有任何用例，`PlanSnapshot` 甚至没被 import —— 只要会话里
    存过计划，打开历史就会 NameError。三处同源（事件 / 落库 / 历史投影）里
    最容易漏的就是第三处：前两处有实时链路盯着，第三处只有刷新才走到。
    """

    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        try:
            store = SessionStore(db)
            await store.add_user_message("u1", "s1", "帮我把接口改完")
            plans = RunPlanStore(db)
            await plans.save(
                "u1",
                "s1",
                "r1",
                3,
                [
                    {
                        "id": "1",
                        "title": "写文件",
                        "status": "done",
                        "verified": True,
                        "evidence": {"path": "src/a.py"},
                    },
                    {"id": "2", "title": "跑测试", "status": "pending"},
                ],
            )

            async def _summary(*args, **kwargs):
                return Summary(total_tokens=0, completion_tokens=0)

            history = History(store, SimpleNamespace(session_summary=_summary), plans)
            conversation = await history.get("u1", "s1")

            assert conversation.plan is not None
            assert conversation.plan.revision == 3
            assert [item.status for item in conversation.plan.items] == [
                "done",
                "pending",
            ]
            # verified 原样透出：UI 靠它区分「已核实完成」与「只是标了完成」
            assert conversation.plan.items[0].verified is True
            assert conversation.plan.items[1].verified is None
        finally:
            await db.close()

    import asyncio

    asyncio.run(go())
