"""阶段 2 验收测试：事件解码器、会话互斥、停止标记与请求校验。

不实际调用 OpenAI API，聚焦可独立验证的纯逻辑。
"""

from __future__ import annotations

from edith_backend.internal.agentrun.execution import build_messages
from edith_backend.internal.agentrun.service import (
    _normalize_request,
    _validate_request,
)
from edith_backend.internal.agentrun.session_lanes import SessionLanes
from edith_backend.internal.agentrun.types import Request
from edith_backend.internal.agentrun.user_stops import UserStops
from edith_backend.internal.agentstream.decoder import StreamDecoder


def test_decoder_allocates_block_per_type_switch() -> None:
    decoder = StreamDecoder("assistant_x")
    first = decoder.content_delta("reasoning", "reasoning.delta", "思考中")
    second = decoder.content_delta("reasoning", "reasoning.delta", "继续思考")
    # 同一类型连续增量共享 BlockID
    assert first.block_id == second.block_id
    assert first.block_type == "reasoning"

    text = decoder.content_delta("text", "message.delta", "你好")
    # 类型切换分配新块
    assert text.block_id != first.block_id
    assert text.block_type == "text"


def test_decoder_tool_events() -> None:
    decoder = StreamDecoder("assistant_x")
    started = decoder.tool_started("call_1", "sandbox_run_command", '{"cmd":"ls"}')
    assert started.type == "tool.started"
    assert started.tool_call_id == "call_1"
    assert started.tool_status == "running"

    finished = decoder.tool_finished("call_1", "completed", "file1 file2")
    assert finished.type == "tool.finished"
    assert finished.tool_status == "completed"


def test_session_lanes_acquire_release() -> None:
    lanes = SessionLanes()
    assert lanes.acquire("u1", "s1", "r1") is True
    # 同一会话第二个任务被拒
    assert lanes.acquire("u1", "s1", "r2") is False
    # 不同会话可并行
    assert lanes.acquire("u1", "s2", "r3") is True

    lanes.release("u1", "s1", "r1")
    assert lanes.acquire("u1", "s1", "r4") is True


def test_user_stops_mark_contains_take() -> None:
    stops = UserStops()
    stops.mark("r1")
    assert stops.contains("r1") is True
    assert stops.take("r1") is True
    assert stops.contains("r1") is False
    assert stops.take("r1") is False


def test_normalize_request_trims_fields() -> None:
    request = Request(
        request_id="  r1  ",
        user_id=" u1 ",
        session_id=" s1 ",
        message=" hi ",
        image_ids=["  img1  "],
    )
    normalized = _normalize_request(request)
    assert normalized.request_id == "r1"
    assert normalized.user_id == "u1"
    assert normalized.session_id == "s1"
    assert normalized.message == "hi"
    assert normalized.image_ids == ["img1"]


def test_validate_request_rules() -> None:
    # 缺必填字段
    assert _validate_request(Request(request_id="", user_id="u", session_id="s", message="hi")) is not None
    # 消息、图片、上传路径全空
    assert _validate_request(Request(request_id="r", user_id="u", session_id="s")) is not None
    # 合法请求
    assert _validate_request(Request(request_id="r", user_id="u", session_id="s", message="hi")) is None


def test_build_messages_order() -> None:
    history = [{"role": "user", "content": "上一条"}, {"role": "assistant", "content": "上一条回复"}]
    # 无摘要：直接拼 history
    messages = build_messages("你是助手", history, [], "", "当前问题")
    assert messages[0] == {"role": "system", "content": "你是助手"}
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "当前问题"}
    # 有摘要：system 追加摘要段，并用 recent_history 取代 history
    messages = build_messages(
        "你是助手",
        history,
        [{"role": "assistant", "content": "最近一条"}],
        "早前聊过天气",
        "当前问题",
    )
    assert messages[0]["role"] == "system"
    assert "早前聊过天气" in messages[0]["content"]
    assert messages[1]["content"] == "最近一条"
    assert messages[-1] == {"role": "user", "content": "当前问题"}
