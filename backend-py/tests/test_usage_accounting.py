"""用量统计的计费正确性测试。

覆盖四层容易同时失效的地方：
1. 流式 usage 块（choices 为空的那一块）必须被捕获 —— 否则永远走本地估算，
   缓存字段恒为未知（这是 token 面板出现 NaN 的根因）。
2. 累加器语义：任一轮未上报 → 整轮缓存未知，不把 None 当 0 求和。
3. 会话汇总：按「已上报缓存」的子集聚合，并暴露覆盖度，
   而不是一次估算就让整会话缓存统计失效。
4. SSE 下发：终态事件的 sessionUsage 必须带上缓存字段（camelCase）。
"""

from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from edith_backend.internal.agentrun.execution import (
    _UsageAccumulator,
    tokens_from_usage,
)
from edith_backend.internal.usage.store import Store
from edith_backend.internal.usage.types import Run, Tokens

# ---------------------------------------------------------------- 累加器语义


def test_accumulator_sums_cache_when_every_round_reports() -> None:
    acc = _UsageAccumulator()
    acc.add(Tokens(prompt_tokens=100, cached_prompt_tokens=40, completion_tokens=10, total_tokens=110))
    acc.add(Tokens(prompt_tokens=200, cached_prompt_tokens=160, completion_tokens=20, total_tokens=220))
    tokens = acc.tokens()
    assert (tokens.prompt_tokens, tokens.completion_tokens, tokens.total_tokens) == (300, 30, 330)
    assert tokens.cached_prompt_tokens == 200
    assert tokens.reported is True


def test_accumulator_marks_cache_unknown_when_any_round_misses_it() -> None:
    """部分轮次没报缓存时，不能把 None 当 0 加（那会低估命中率）。"""
    acc = _UsageAccumulator()
    acc.add(Tokens(prompt_tokens=100, cached_prompt_tokens=40, completion_tokens=10, total_tokens=110))
    acc.add(Tokens(prompt_tokens=200, cached_prompt_tokens=None, completion_tokens=20, total_tokens=220))
    assert acc.tokens().cached_prompt_tokens is None


def test_accumulator_propagates_estimated_flag() -> None:
    acc = _UsageAccumulator()
    acc.add(Tokens(prompt_tokens=10, cached_prompt_tokens=0, completion_tokens=1, total_tokens=11))
    acc.add(
        Tokens(
            prompt_tokens=10,
            cached_prompt_tokens=None,
            completion_tokens=1,
            total_tokens=11,
            reported=False,
        )
    )
    tokens = acc.tokens()
    assert tokens.reported is False
    assert tokens.total_tokens == 22


def test_accumulator_empty_stays_unknown() -> None:
    tokens = _UsageAccumulator().tokens()
    assert tokens.total_tokens == 0
    assert tokens.cached_prompt_tokens is None


def test_tokens_from_usage_reads_prompt_cache_details() -> None:
    class _Details:
        cached_tokens = 128

    class _Usage:
        prompt_tokens = 512
        completion_tokens = 64
        total_tokens = 576
        prompt_tokens_details = _Details()

    tokens = tokens_from_usage(_Usage())
    assert tokens.cached_prompt_tokens == 128
    assert tokens.reported is True


# ---------------------------------------------------------------- 会话汇总

async def _store() -> tuple[Store, aiosqlite.Connection]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    store = Store(db)
    await store.create_schema()
    return store, db


def test_session_summary_aggregates_reported_subset_only() -> None:
    async def scenario() -> None:
        store, db = await _store()
        try:
            # 一次真实上报缓存
            await store.start(Run("r1", "u1", "s1", "m1"))
            await store.finish(
                Run("r1", "u1", "s1", "m1"),
                Tokens(
                    prompt_tokens=400,
                    cached_prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=450,
                ),
            )
            # 一次没报缓存（本地估算）
            await store.start(Run("r2", "u1", "s1", "m1"))
            await store.finish(
                Run("r2", "u1", "s1", "m1"),
                Tokens(
                    prompt_tokens=600,
                    cached_prompt_tokens=None,
                    completion_tokens=50,
                    total_tokens=650,
                    reported=False,
                ),
            )
            summary = await store.session_summary("u1", "s1")
            assert summary.total_tokens == 1100
            assert summary.completion_tokens == 100
            assert summary.total_runs == 2
            assert summary.cache_reported_runs == 1
            assert summary.estimated_runs == 1
            # 命中率只在「上报过的那一次」上算：100 / 400
            assert summary.cached_prompt_tokens == 100
            assert summary.uncached_prompt_tokens == 300
            assert summary.cache_hit_rate == pytest.approx(0.25)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_session_summary_without_any_cache_report_returns_unknown() -> None:
    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.start(Run("r1", "u1", "s1", "m1"))
            await store.finish(
                Run("r1", "u1", "s1", "m1"),
                Tokens(
                    prompt_tokens=100,
                    cached_prompt_tokens=None,
                    completion_tokens=10,
                    total_tokens=110,
                    reported=False,
                ),
            )
            summary = await store.session_summary("u1", "s1")
            assert summary.total_tokens == 110
            assert summary.cached_prompt_tokens is None
            assert summary.uncached_prompt_tokens is None
            assert summary.cache_hit_rate is None
            assert summary.cache_reported_runs == 0
            assert summary.estimated_runs == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_session_summary_treats_zero_cache_as_known() -> None:
    """cached_tokens=0 是「真实命中 0」，不是「未知」。"""

    async def scenario() -> None:
        store, db = await _store()
        try:
            await store.start(Run("r1", "u1", "s1", "m1"))
            await store.finish(
                Run("r1", "u1", "s1", "m1"),
                Tokens(
                    prompt_tokens=100,
                    cached_prompt_tokens=0,
                    completion_tokens=10,
                    total_tokens=110,
                ),
            )
            summary = await store.session_summary("u1", "s1")
            assert summary.cached_prompt_tokens == 0
            assert summary.uncached_prompt_tokens == 100
            assert summary.cache_hit_rate == 0.0
        finally:
            await db.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------- SSE 全链路


class _Delta:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.reasoning_content = None
        self.model_extra = {}


class _Choice:
    def __init__(self, delta: _Delta) -> None:
        self.delta = delta


class _CacheDetails:
    cached_tokens = 30


class _Usage:
    """模拟百炼：真实用量只在 choices 为空的收尾块里。"""

    prompt_tokens = 40
    completion_tokens = 5
    total_tokens = 45
    prompt_tokens_details = _CacheDetails()


class _Chunk:
    def __init__(self, delta: _Delta | None = None, usage: _Usage | None = None) -> None:
        # usage 收尾块的 choices 一定是空列表 —— 这正是旧实现提前 continue 的地方
        self.choices = [] if delta is None else [_Choice(delta)]
        self.usage = usage


class _Stream:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _Completions:
    async def create(self, **kwargs):
        return _Stream(
            [
                _Chunk(_Delta("你好")),
                _Chunk(usage=_Usage()),
            ]
        )


class _Chat:
    completions = _Completions()


class _FakeAsyncOpenAI:
    def __init__(self, **kwargs) -> None:
        self.chat = _Chat()


def test_sse_session_usage_includes_cached_tokens(client, monkeypatch) -> None:
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-usage",
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
            "requestId": "req-usage-1",
            "userId": "u-usage",
            "sessionId": "s-usage",
            "message": "你好",
            "modelId": "minimax.m3",
        },
    )
    assert response.status_code == 200

    completed = None
    for line in response.text.splitlines():
        if line.startswith('data: {"type":"run.completed"'):
            completed = json.loads(line[len("data: ") :])
    assert completed is not None, response.text

    usage = completed["sessionUsage"]
    # 关键回归：usage 收尾块被捕获后，prompt/缓存/命中率都必须有值
    assert usage["totalTokens"] == 45
    assert usage["completionTokens"] == 5
    assert usage["cachedPromptTokens"] == 30
    assert usage["uncachedPromptTokens"] == 10
    assert usage["cacheHitRate"] == pytest.approx(0.75)
    assert usage["totalRuns"] == 1
    assert usage["cacheReportedRuns"] == 1
    assert usage["estimatedRuns"] == 0
