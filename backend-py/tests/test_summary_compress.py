"""会话摘要压缩（summary_compress）的回归测试。

覆盖四条口径：
1. 旧摘要必须参与新摘要生成（曾整体覆盖上一代 → 早期信息硬丢失）；
2. 按 turn 对齐切分（最近窗口保持完整 turn，不拆 tool_calls/tool、不拆一问一答）；
3. 失败降级有界（硬截断到「旧摘要 + 最近窗口」且不写库），溢出重试不丢最旧信息；
4. 摘要独立成 system 消息（可按结构定位），并对「只接受单 system」的端点有合并降级。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite
import httpx
from openai import APIStatusError

from edith_backend.internal.agentrun import execution
from edith_backend.internal.agentrun.configuration import ConfiguredRun
from edith_backend.internal.agentrun.execution import (
    SUMMARY_TAG,
    _estimate_tokens,
    _first_model_call,
    _recent_msgs,
    _split_into_turns,
    collapse_system_messages,
    is_summary_message,
    summary_compress,
)
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.trace.tracer import new_tracer

OLD_SUMMARY = "早期关键决策：服务接口统一命名为 FooService，技术选型为 Python + FastAPI。"


def _configured(message: str = "当前消息") -> ConfiguredRun:
    # context_window 故意压小：threshold = 50 × 0.4 = 20，少量消息即可触发压缩
    return ConfiguredRun(
        request=SimpleNamespace(user_id="u1", session_id="s1"),
        definition=SimpleNamespace(api_model="test-model", context_window=50),
        api_key="sk-test",
        system_instruction="",
        image_urls=[],
        message=message,
    )


def _fake_client(captured: dict, reply: str = "更新后的摘要"):
    class _Completions:
        async def create(self, **kwargs):
            captured["calls"] = captured.get("calls", []) + [kwargs]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                usage=None,
            )

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(chat=_Chat())


def _scripted_client(captured: dict, script: list):
    """按脚本依次返回回复或抛异常，用于验证重试/降级路径。"""

    class _Completions:
        async def create(self, **kwargs):
            captured["calls"] = captured.get("calls", []) + [kwargs]
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=item))],
                usage=None,
            )

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(chat=_Chat())


async def _fresh_store() -> tuple[aiosqlite.Connection, SessionStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, SessionStore(db)


def test_old_summary_included_in_prompt() -> None:
    """已有摘要时，prompt 必须带旧摘要全文（修复的核心断言）。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            # 旧摘要 + 12 条新历史（12 个 turn，超过 MIN_SUMMARY_TURNS）
            await store.save_summary("u1", "s1", 0, OLD_SUMMARY)
            for i in range(12):
                await store.add_user_message("u1", "s1", f"第{i}轮：继续推进任务 {i}")

            captured: dict = {}
            tracer = new_tracer(None, request_id="r1", user_id="u1", session_id="s1")
            messages, _tokens = await summary_compress(
                _configured(), store, "u1", "s1", _fake_client(captured), tracer
            )

            assert captured["calls"], "摘要调用未触发"
            prompt_text = str(captured["calls"][0]["messages"])
            # 旧摘要全文进 prompt——这是修复点
            assert "FooService" in prompt_text, prompt_text
            assert "上一份摘要" in prompt_text
            # 待摘要范围是最早的 11 条：最近 1 个 turn 留在窗口里，不参与摘要
            assert "第10轮" in prompt_text
            assert "第11轮" not in prompt_text
            # 模型回复成为新摘要并保存
            saved = await store.get_summary("u1", "s1")
            assert saved is not None and saved["summary"] == "更新后的摘要"
            # 返回的 messages 以 system 开头、带当前用户消息，最近窗口仍是原文
            assert messages[0]["role"] == "system"
            assert messages[-1]["content"] == "当前消息"
            assert any("第11轮" in m["content"] for m in messages)
        finally:
            await db.close()

    asyncio.run(go())


def test_first_compression_uses_structured_prompt() -> None:
    """首次压缩（无旧摘要）：走 INITIAL 结构化模板，而不是自由文本要点。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            for i in range(12):
                await store.add_user_message("u1", "s1", f"第{i}轮：任务内容 {i}")

            captured: dict = {}
            tracer = new_tracer(None, request_id="r2", user_id="u1", session_id="s1")
            await summary_compress(
                _configured(), store, "u1", "s1", _fake_client(captured), tracer
            )

            assert captured["calls"]
            prompt_text = str(captured["calls"][0]["messages"])
            assert "## 目标" in prompt_text
            assert "## 未完成 / 待办" in prompt_text
            assert "上一份摘要" not in prompt_text
        finally:
            await db.close()

    asyncio.run(go())


def test_below_min_turns_no_summary_call() -> None:
    """turn 数不足（没有可折叠的中段）：不触发摘要调用，messages 直接组装。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            for i in range(3):
                await store.add_user_message("u1", "s1", f"短消息 {i}")

            captured: dict = {}
            tracer = new_tracer(None, request_id="r3", user_id="u1", session_id="s1")
            messages, tokens = await summary_compress(
                _configured(), store, "u1", "s1", _fake_client(captured), tracer
            )

            assert "calls" not in captured, "未达阈值不应触发摘要调用"
            assert tokens is None
            # system 提示 + 3 条历史 + 当前消息，全部保留原文
            assert len(messages) == 5
        finally:
            await db.close()

    asyncio.run(go())


def test_split_into_turns_never_breaks_tool_pairs() -> None:
    """assistant(tool_calls) 与它的 tool 结果必须落在同一个 turn 里。"""
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "content": "r1"},
        {"role": "tool", "content": "r2"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]

    turns = _split_into_turns(messages)

    assert [len(turn) for turn in turns] == [5, 1]
    assert turns[0][0]["content"] == "q1"


def test_recent_msgs_keeps_complete_turns() -> None:
    """最近窗口按 turn 计数，且至少保留最后一个完整 turn。"""
    messages = [
        {"role": role, "content": f"第{i}轮{tag}"}
        for i in range(5)
        for role, tag in (("user", "问"), ("assistant", "答"))
    ]

    recent = _recent_msgs(messages, keep_tokens=10**6, max_turns=2)
    assert [m["content"] for m in recent] == ["第3轮问", "第3轮答", "第4轮问", "第4轮答"]

    # 预算再小也不能把当前这轮丢掉
    assert len(_recent_msgs(messages, keep_tokens=1)) == 2


def test_estimate_tokens_counts_tool_call_payload() -> None:
    """tool_calls 的 JSON 也是上下文开销：只算 content 会严重低估。"""
    plain = _estimate_tokens([{"role": "user", "content": "hi"}])
    with_tool_call = _estimate_tokens(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "f", "arguments": "x" * 4000}}],
            }
        ]
    )
    assert with_tool_call >= 1000 > plain


def test_summary_failure_degrades_to_hard_trim() -> None:
    """摘要调用失败：降级为「旧摘要 + 最近窗口」，且不写库。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.save_summary("u1", "s1", 0, OLD_SUMMARY)
            for i in range(12):
                await store.add_user_message("u1", "s1", f"第{i}轮：继续推进任务 {i}")

            captured: dict = {}
            tracer = new_tracer(None, request_id="r4", user_id="u1", session_id="s1")
            messages, tokens = await summary_compress(
                _configured(),
                store,
                "u1",
                "s1",
                _scripted_client(captured, [RuntimeError("boom")]),
                tracer,
            )

            assert captured["calls"], "摘要调用应已尝试"
            assert tokens is None
            # 旧摘要独立成条 system（messages[1]），其后只有最近窗口（1 个 turn）与当前消息
            assert is_summary_message(messages[1])
            assert "FooService" in messages[1]["content"]
            assert [m["content"] for m in messages[2:-1]] == ["第11轮：继续推进任务 11"]
            assert messages[-1]["content"] == "当前消息"
            # 不写库：摘要与游标都保持不变
            saved = await store.get_summary("u1", "s1")
            assert saved is not None
            assert saved["summary"] == OLD_SUMMARY
            assert saved["summarized_until_id"] == 0
        finally:
            await db.close()

    asyncio.run(go())


def test_overflow_retry_keeps_newer_half_in_history() -> None:
    """溢出重试只摘要「较旧的前缀」，较新的一半仍留在历史里，不静默丢弃。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            ids = [
                await store.add_user_message("u1", "s1", f"第{i}轮：任务内容 {i}")
                for i in range(12)
            ]

            captured: dict = {}
            tracer = new_tracer(None, request_id="r5", user_id="u1", session_id="s1")
            messages, _ = await summary_compress(
                _configured(),
                store,
                "u1",
                "s1",
                _scripted_client(
                    captured,
                    [
                        RuntimeError("maximum context length is 8192 tokens"),
                        "溢出后的摘要",
                    ],
                ),
                tracer,
            )

            assert len(captured["calls"]) == 2
            saved = await store.get_summary("u1", "s1")
            assert saved is not None
            # 待摘要 11 条 → 溢出后砍半为 5 条 → 游标停在前 5 条的最后一条
            assert saved["summarized_until_id"] == ids[4]
            # 未被摘要的 5~11 条仍在上下文里——旧实现会把它们一并标记为已折叠而丢失
            contents = [m["content"] for m in messages]
            assert "第5轮：任务内容 5" in contents
            assert "第10轮：任务内容 10" in contents
        finally:
            await db.close()

    asyncio.run(go())


# ===== 摘要消息形态（独立 system 条） =====

_DEFINITION = SimpleNamespace(
    id="test.model",
    api_model="test-model",
    base_url="http://test.local/v1",
    extra_fields=None,
    context_window=8192,
)


def _wire_configured() -> ConfiguredRun:
    return ConfiguredRun(
        request=SimpleNamespace(user_id="u1", session_id="s1", request_id="r-wire"),
        definition=_DEFINITION,
        api_key="sk-test",
        system_instruction="你是助手",
        image_urls=[],
        message="当前消息",
    )


def _status_error(status: int) -> APIStatusError:
    request = httpx.Request("POST", "http://test.local/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return APIStatusError("boom", response=response, body=None)


def _recording_client(sent: list[list[dict]], fail_status: int | None):
    class _Completions:
        async def create(self, **kwargs):
            sent.append([dict(m) for m in kwargs["messages"]])
            if fail_status is not None and len(sent) == 1:
                raise _status_error(fail_status)
            return "stream"

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    return _FakeAsyncOpenAI


def test_summary_rendered_as_separate_system_message() -> None:
    """有摘要时：系统指令原样留在 messages[0]，摘要是紧随其后的独立 system 条。"""

    async def go() -> None:
        db, store = await _fresh_store()
        try:
            await store.save_summary("u1", "s1", 0, OLD_SUMMARY)
            for i in range(12):
                await store.add_user_message("u1", "s1", f"第{i}轮：继续推进任务 {i}")

            tracer = new_tracer(None, request_id="r6", user_id="u1", session_id="s1")
            messages, _ = await summary_compress(
                _wire_configured(), store, "u1", "s1", _fake_client({}), tracer
            )

            # 指令未被改写：摘要不再拼进指令正文
            assert messages[0] == {"role": "system", "content": "你是助手"}
            # 摘要独立成条，可按首行标记定位
            assert is_summary_message(messages[1])
            assert "FooService" in messages[1]["content"]
            assert messages[-1]["role"] == "user"
        finally:
            await db.close()

    asyncio.run(go())


def test_collapse_system_messages_keeps_order_and_content() -> None:
    """合并只动 system 条：内容全保留、相对顺序不变、非 system 消息不挪位。"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "system", "content": f"{SUMMARY_TAG} 头部\n旧摘要"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    merged = collapse_system_messages(messages)
    assert len(merged) == 4
    assert merged[0]["role"] == "system"
    assert "你是助手" in merged[0]["content"]
    assert "旧摘要" in merged[0]["content"]
    # 顺序：指令在前、摘要在后
    assert merged[0]["content"].index("你是助手") < merged[0]["content"].index("旧摘要")
    assert [m["content"] for m in merged[1:]] == ["第一问", "第一答", "第二问"]
    # 只有一条 system 时原样返回（不引入无意义改写）
    only_one = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
    assert collapse_system_messages(only_one) == only_one


def test_first_model_call_collapses_on_multi_system_rejection(monkeypatch) -> None:
    """端点拒收多条 system（4xx）→ 合并成一条就地重试，并写回后续轮次共用的列表。"""
    sent: list[list[dict]] = []
    monkeypatch.setattr(
        execution, "AsyncOpenAI", _recording_client(sent, fail_status=400)
    )
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "system", "content": f"{SUMMARY_TAG} 头部\n旧摘要内容"},
        {"role": "user", "content": "你好"},
    ]
    attempts: list[dict] = []
    _, _, stream = asyncio.run(
        _first_model_call(_wire_configured(), None, messages, [], attempts)
    )

    assert stream == "stream"
    assert len(sent) == 2
    assert sum(1 for m in sent[0] if m["role"] == "system") == 2
    assert sum(1 for m in sent[1] if m["role"] == "system") == 1
    # 合并写回原列表 —— 后续工具轮次直接复用，不需要逐轮降级
    assert sum(1 for m in messages if m["role"] == "system") == 1
    assert attempts and attempts[0]["form"] == "collapsed_system"


def test_first_model_call_does_not_collapse_when_not_a_shape_problem(
    monkeypatch,
) -> None:
    """只有「4xx + 多条 system」才合并：单 system 的 4xx 与 5xx 都原样抛出。"""
    for status, system_count in ((400, 1), (500, 2)):
        sent: list[list[dict]] = []
        monkeypatch.setattr(
            execution, "AsyncOpenAI", _recording_client(sent, fail_status=status)
        )
        messages = [{"role": "system", "content": "你是助手"}] * system_count + [
            {"role": "user", "content": "你好"}
        ]
        attempts: list[dict] = []
        try:
            asyncio.run(_first_model_call(_wire_configured(), None, messages, [], attempts))
        except APIStatusError as exc:
            assert exc.status_code == status
        else:  # pragma: no cover - 失败路径必须抛出
            raise AssertionError("应当抛出 APIStatusError")
        assert len(sent) == 1, "不该重试"
        assert not any(a.get("form") == "collapsed_system" for a in attempts)
