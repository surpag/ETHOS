"""完成判定修复（P0 假完成路径）的回归测试。

背景：finish_reason 此前全项目从未读取。输出被供应商 max_tokens 硬截断
（finish_reason="length"）时，模型是被外力打断、不是它自己决定收尾——
旧实现按「本轮无工具调用」直接判 completed 并清空 checkpoint（假完成）。

问题空间枚举（finish_reason × tool_calls）：
| finish_reason   | 无工具调用            | 有工具调用                  |
|-----------------|----------------------|---------------------------|
| stop            | completed（原行为）    | 执行工具（原行为）          |
| tool_calls      | —                    | 执行工具（原行为）          |
| length          | output_truncated      | output_truncated，不执行   |
|                 |                      | 残缺调用（参数流被砍断）     |
| content_filter  | content_filtered      | 执行工具，end_reason 标记   |
| 未上报          | completed（原行为）    | 执行工具（原行为）          |

端到端断言两条链路：
1. SSE 终态事件的 endReason 字段（UI 可见）
2. DB 的 session_meta.task_status（恢复依据：截断必须停留 in_progress 供续跑）
"""

from __future__ import annotations

import json
import os
import sqlite3

# ---------------------------------------------------------------- SSE mock 桩


class _Delta:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.reasoning_content = None
        self.model_extra = {}
        self.tool_calls = None


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _CacheDetails:
    cached_tokens = 0


class _Usage:
    prompt_tokens = 40
    completion_tokens = 5
    total_tokens = 45
    prompt_tokens_details = _CacheDetails()


class _Chunk:
    def __init__(
        self,
        delta: _Delta | None = None,
        usage: _Usage | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.choices = [] if delta is None else [_Choice(delta, finish_reason)]
        self.usage = usage


class _Stream:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def _client_with(chunks: list[_Chunk]):
    """按流块序列构造 AsyncOpenAI 替身（复用既有测试的 mock 模式）。"""

    class _Completions:
        async def create(self, **kwargs):
            return _Stream(chunks)

    class _Chat:
        completions = _Completions()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = _Chat()

    return _FakeAsyncOpenAI


def _run_message(client, monkeypatch, user_id: str, session_id: str, fake) -> dict:
    """注入模型替身后发一条消息，返回解析后的终态事件。"""
    client.put(
        "/internal/user-settings",
        json={
            "userId": user_id,
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "minimax", "apiKey": "sk-test"}],
        },
    )
    monkeypatch.setattr(
        "edith_backend.internal.agentrun.execution.AsyncOpenAI",
        fake,
    )
    response = client.post(
        "/internal/gateway/messages:stream",
        json={
            "requestId": f"req-{session_id}",
            "userId": user_id,
            "sessionId": session_id,
            "message": "你好",
            "modelId": "minimax.m3",
        },
    )
    assert response.status_code == 200, response.text
    completed = None
    for line in response.text.splitlines():
        if line.startswith('data: {"type":"run.completed"'):
            completed = json.loads(line[len("data: ") :])
    assert completed is not None, response.text
    return completed


def _task_status(user_id: str, session_id: str) -> str | None:
    """直连测试库（conftest 已把 EDITH_DB_PATH 指向临时文件）读任务状态。"""
    conn = sqlite3.connect(os.environ["EDITH_DB_PATH"])
    try:
        row = conn.execute(
            "SELECT task_status FROM session_meta WHERE user_id=? AND session_id=?",
            (user_id, session_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 用例


def test_length_truncation_not_marked_completed(client, monkeypatch) -> None:
    """length 截断：end_reason=output_truncated，task_status 停留 in_progress。"""
    fake = _client_with(
        [
            # 最后一个带 choices 的块携带 finish_reason（协议约定）
            _Chunk(_Delta("写了一半的内容"), finish_reason="length"),
            _Chunk(usage=_Usage()),
        ]
    )
    completed = _run_message(client, monkeypatch, "u-trunc", "s-trunc", fake)

    assert completed["endReason"] == "output_truncated", completed
    # 恢复依据：不清 checkpoint 的前提是不判完成 → 状态必须停留 in_progress
    assert _task_status("u-trunc", "s-trunc") == "in_progress"


def test_normal_stop_still_completed(client, monkeypatch) -> None:
    """正常 stop：end_reason=completed、task_status=completed（回归保护）。"""
    fake = _client_with(
        [
            _Chunk(_Delta("你好！")),
            _Chunk(_Delta(""), finish_reason="stop"),
            _Chunk(usage=_Usage()),
        ]
    )
    completed = _run_message(client, monkeypatch, "u-normal", "s-normal", fake)

    assert completed["endReason"] == "completed", completed
    assert _task_status("u-normal", "s-normal") == "completed"


def test_content_filter_not_completed(client, monkeypatch) -> None:
    """content_filter：外力打断同样不判完成（end_reason 单独标记）。"""
    fake = _client_with(
        [
            _Chunk(_Delta("部分内容"), finish_reason="content_filter"),
            _Chunk(usage=_Usage()),
        ]
    )
    completed = _run_message(client, monkeypatch, "u-filter", "s-filter", fake)

    assert completed["endReason"] == "content_filtered", completed
    assert _task_status("u-filter", "s-filter") == "in_progress"


def test_missing_finish_reason_keeps_legacy_behavior(client, monkeypatch) -> None:
    """供应商流不带 finish_reason（兼容桩无该属性）：维持原判定，不改变行为。"""

    class _BareChoice:
        """模拟不规范供应商流：choices[0] 无 finish_reason 字段。"""

        def __init__(self, delta: _Delta) -> None:
            self.delta = delta
            self.usage = None

    class _BareChunk:
        def __init__(self, delta: _Delta | None = None, usage: _Usage | None = None):
            self.choices = [] if delta is None else [_BareChoice(delta)]
            self.usage = usage

    fake = _client_with(
        [
            _BareChunk(_Delta("正常输出")),
            _BareChunk(usage=_Usage()),
        ]
    )
    completed = _run_message(client, monkeypatch, "u-bare", "s-bare", fake)

    assert completed["endReason"] == "completed", completed
    assert _task_status("u-bare", "s-bare") == "completed"
