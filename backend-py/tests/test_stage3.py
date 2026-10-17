"""阶段 3 验收测试：gateway 渠道翻译 + webadapter SSE 流式链路。

SSE 测试用 mock 的 AsyncOpenAI 验证 run.started → message.delta → run.completed 完整链路。
client fixture 由 conftest.py 提供（session 级，共享单次 lifespan）。
"""

from __future__ import annotations

import json


class _FakeDelta:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.reasoning_content = None
        self.model_extra = {}


class _FakeChoice:
    def __init__(self, delta: _FakeDelta) -> None:
        self.delta = delta


class _FakeUsage:
    total_tokens = 10
    completion_tokens = 5
    prompt_tokens = 5
    prompt_tokens_details = None


class _FakeChunk:
    def __init__(self, delta: _FakeDelta, usage: _FakeUsage | None = None) -> None:
        self.choices = [_FakeChoice(delta)]
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _FakeCompletions:
    async def create(self, **kwargs):
        return _FakeStream(
            [
                _FakeChunk(_FakeDelta("你好")),
                _FakeChunk(_FakeDelta("，世界")),
                _FakeChunk(_FakeDelta(), usage=_FakeUsage()),
            ]
        )


class _FakeChat:
    completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, **kwargs) -> None:
        self.chat = _FakeChat()


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_stream_message_missing_user_id_returns_400(client) -> None:
    response = client.post(
        "/internal/gateway/messages:stream",
        json={"requestId": "r1", "userId": "", "sessionId": "s1", "message": "hi"},
    )
    assert response.status_code == 400


def test_stream_message_sse(client, monkeypatch) -> None:
    # 配置 API key 使模型可用
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-sse",
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "minimax", "apiKey": "sk-test"}],
        },
    )
    monkeypatch.setattr(
        "edith_backend.internal.agentrun.execution.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    response = client.post(
        "/internal/gateway/messages:stream",
        json={
            "requestId": "req-1",
            "userId": "u-sse",
            "sessionId": "s-sse",
            "message": "你好",
            "modelId": "minimax.m3",
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    # 校验 SSE 帧：run.started → message.delta → run.completed
    assert 'data: {"type":"run.started"' in body
    assert '"type":"message.delta"' in body
    assert '"delta":"你好"' in body
    assert '"type":"run.completed"' in body
    # 事件必须使用 camelCase 字段（对齐 Go 版 agentstream.Event 的 json tag），
    # 前端 applyStreamEvent 依赖 assistantId/blockId/blockType/sessionId/requestId。
    # 回归：model_dump_json 缺省 by_alias=False 会输出 snake_case，
    # 导致前端读到 undefined 的 key 并触发 React “unique key” 警告。
    assert '"assistantId":"assistant_req-1"' in body
    assert '"blockId":"assistant_req-1_text_1"' in body
    assert '"blockType":"text"' in body
    assert '"sessionId":"s-sse"' in body
    assert '"requestId":"req-1"' in body
    # run.completed 的用量字段序列化为 sessionUsage（对齐 Go json tag 与前端类型）
    assert '"type":"run.completed"' in body
    # 缓存字段缺失时（本 fake 未上报 prompt_tokens_details）由 exclude_none 省略，
    # 但覆盖度字段恒在，供前端判断「这是全量统计还是局部统计」
    assert (
        '"sessionUsage":{"totalTokens":10,"completionTokens":5,'
        '"totalRuns":1,"cacheReportedRuns":0,"estimatedRuns":0}'
    ) in body

    # 完成时长：终态事件携带 durationMs（camelCase）与 assistantId，
    # 前端据此把耗时写到本次运行的助手块上。
    frames = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and '"run.completed"' in line
    ]
    assert len(frames) == 1
    completed = frames[0]
    assert completed["assistantId"] == "assistant_req-1"
    assert completed["endReason"] == "completed"
    assert isinstance(completed["durationMs"], int) and completed["durationMs"] >= 0

    # 同一数值落库：刷新会话历史（走 conversation 投影）仍能看到该耗时
    detail = client.get("/internal/conversations/s-sse", params={"userId": "u-sse"}).json()
    assistants = [b for b in detail["timeline"]["blocks"] if b["type"] == "assistant"]
    assert assistants[-1]["durationMs"] == completed["durationMs"]


def test_run_not_found_returns_404(client) -> None:
    # 未知 requestId 查询返回 404，验证 gateway 错误映射
    response = client.get("/internal/gateway/runs/unknown-req?userId=u-sse")
    assert response.status_code == 404
    assert response.json() == {"type": "not_found", "message": "agent run not found"}


def _wait_trace(request_id: str) -> tuple[dict, list[dict]]:
    """轮询等待 trace 收尾。

    SSE 流的结束哨兵在 trace 收尾之前发出（自动记忆提取可能还要跑几秒），
    因此响应返回时 trace 未必已 finished——这里给一个小窗口。
    """
    import os
    import sqlite3
    import time

    path = os.environ["EDITH_DB_PATH"]
    for _ in range(100):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM traces WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None and row["finished_at"]:
                steps = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM trace_steps WHERE request_id = ? ORDER BY seq",
                        (request_id,),
                    ).fetchall()
                ]
                return dict(row), steps
        finally:
            conn.close()
        time.sleep(0.05)
    raise AssertionError(f"trace {request_id} 未在预期时间内收尾")


def test_trace_recorded_for_stream_run(client, monkeypatch) -> None:
    """链路追踪端到端：一次真实 run 后能查到 traces + trace_steps。

    验证的是「真实装配」（main.py 的 Dependencies 已接上 trace store），
    而非单独构造的 Tracer。
    """
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-trace",
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "minimax", "apiKey": "sk-test"}],
        },
    )
    monkeypatch.setattr(
        "edith_backend.internal.agentrun.execution.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    response = client.post(
        "/internal/gateway/messages:stream",
        json={
            "requestId": "req-trace-1",
            "userId": "u-trace",
            "sessionId": "s-trace",
            "message": "你好",
            "modelId": "minimax.m3",
        },
    )
    assert response.status_code == 200

    trace, steps = _wait_trace("req-trace-1")
    assert trace["status"] == "ok"
    assert trace["end_reason"] == "completed"
    assert trace["user_id"] == "u-trace"
    assert trace["model_id"] == "minimax.m3"
    assert trace["duration_ms"] is not None and trace["duration_ms"] >= 0
    assert trace["error_type"] is None

    types = [s["type"] for s in steps]
    assert "prepare" in types
    assert "llm_call" in types

    # 准备阶段拆成了子步骤：model.resolve 必须挂在 load_configuration 之下（ContextVar 父子关联）
    parent = next(s for s in steps if s["name"] == "load_configuration")
    child = next(s for s in steps if s["name"] == "model.resolve")
    assert child["parent_seq"] == parent["seq"]
    assert child["depth"] == 1

    # LLM 步必须带 TTFT（「LLM 一直在思考」的定位依据）
    llm = next(s for s in steps if s["type"] == "llm_call")
    meta = json.loads(llm["metadata"])
    assert meta["round"] == 1
    assert "ttft_ms" in meta and meta["ttft_ms"] >= 0
    assert meta["content_chars"] == len("你好，世界")
