"""缓存诊断埋点的回归测试：每轮请求必须留下可定责的事实。

背景：此前只有 run 级汇总（`usage_runs` 一行压几十轮），于是「跨 run 前缀
复用率」只能靠两次 run 的累加值相减估算 —— 那个算法是错的（测出来的是总量
差，不是复用率）。要定案，必须逐轮记录：

| 字段       | 回答什么问题                                    |
|-----------|-----------------------------------------------|
| prompt    | 这一轮发出去多少 token                           |
| cached    | 其中多少命中缓存（**None 与 0 语义不同**）        |
| sys_hash  | system 段有没有变（变了 = 我们改写了请求头）       |
| oversized | 历史里是否仍有超预算的工具结果（只读审计，正常 0） |

最关键的一条：`cached=None`（供应商没上报该字段）绝不能和 `cached=0`
（上报了、真的没命中）混为一谈。混了就会把「统计假象」当成「缓存很差」，
然后去优化一个不存在的指标。
"""

from __future__ import annotations

import json
import os
import sqlite3

# ---------------------------------------------------------------- mock 桩


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
    def __init__(self, cached: int) -> None:
        self.cached_tokens = cached


class _Usage:
    """可控制「有无 prompt_tokens_details」的用量块（两种供应商行为都要覆盖）。"""

    def __init__(self, prompt: int = 4000, cached: int | None = 0) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = 5
        self.total_tokens = prompt + 5
        self.prompt_tokens_details = None if cached is None else _CacheDetails(cached)


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
    class _Completions:
        async def create(self, **kwargs):
            return _Stream(chunks)

    class _Chat:
        completions = _Completions()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = _Chat()

    return _FakeAsyncOpenAI


def _run_once(
    client,
    monkeypatch,
    user_id: str,
    session_id: str,
    usage: _Usage,
    settings: dict | None = None,
) -> None:
    payload = {
        "userId": user_id,
        "defaultModelId": "minimax.m3",
        "providers": [{"providerId": "minimax", "apiKey": "sk-test"}],
    }
    if settings:
        payload.update(settings)
    client.put("/internal/user-settings", json=payload)
    monkeypatch.setattr(
        "edith_backend.internal.agentrun.execution.AsyncOpenAI",
        _client_with([_Chunk(_Delta("好的")), _Chunk(usage=usage)]),
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


def _llm_calls(request_id: str) -> list[dict]:
    """按顺序取该 run 的 llm_call 步骤 metadata。"""
    conn = sqlite3.connect(os.environ["EDITH_DB_PATH"])
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT metadata FROM trace_steps WHERE request_id=? AND type='llm_call' "
            "ORDER BY seq",
            (request_id,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["metadata"] or "{}") for row in rows]


def test_llm_call_records_prompt_and_cached(client, monkeypatch) -> None:
    """每轮都记 prompt / cached / sys_hash / oversized。"""
    _run_once(client, monkeypatch, "u-tel", "s-tel", _Usage(prompt=4000, cached=0))

    steps = _llm_calls("req-s-tel")
    assert steps, "未记录 llm_call 步骤"
    first = steps[0]
    assert first["prompt"] == 4000
    # 供应商上报了 0：这是「真的没命中」，必须与「未上报」区分
    assert first["cached"] == 0
    assert isinstance(first["sys_hash"], str) and len(first["sys_hash"]) == 12
    assert first["sys_chars"] > 0
    # 历史不可变：没有「就地改写旧工具结果」这回事，这个值恒为 0
    assert first["oversized"] == 0


def test_vendor_without_cache_details_is_not_reported_as_zero(
    client, monkeypatch
) -> None:
    """供应商不返回 prompt_tokens_details：cached 必须是 None（未知），不能伪造成 0。"""
    _run_once(
        client, monkeypatch, "u-tel-none", "s-tel-none", _Usage(cached=None)
    )

    first = _llm_calls("req-s-tel-none")[0]
    assert "cached" in first, "字段必须存在（null 也是一条事实）"
    assert first["cached"] is None


def test_sys_hash_differs_when_system_prompt_changes(client, monkeypatch) -> None:
    """同一个用户两次 run：改掉用户设置里的性格/记忆后 sys_hash 必须变化。

    这是「system 段指纹」的意义所在 —— 它变化就说明破坏半径覆盖整个历史，
    与模型行为无关。反过来，设置不变时两次 run 的 sys_hash 应当一致。
    """
    _run_once(client, monkeypatch, "u-tel-sys", "s-tel-a", _Usage())
    _run_once(client, monkeypatch, "u-tel-sys", "s-tel-b", _Usage())
    a = _llm_calls("req-s-tel-a")[0]["sys_hash"]
    b = _llm_calls("req-s-tel-b")[0]["sys_hash"]
    assert a == b, f"同一配置下 system 段不该漂移：{a} vs {b}"

    # 改掉用户性格（直接进 system 指令）→ 指纹必须变
    _run_once(
        client,
        monkeypatch,
        "u-tel-sys",
        "s-tel-c",
        _Usage(),
        settings={"personality": "用非常简短的方式回答"},
    )
    c = _llm_calls("req-s-tel-c")[0]["sys_hash"]
    assert c != a, "改了用户配置却没反映到 sys_hash，指纹失效"
