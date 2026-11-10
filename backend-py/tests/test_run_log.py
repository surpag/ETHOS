"""事件日志与断流重连。

这里的核心不是「能不能读到事件」，而是**刷新页面后还能不能读到**：
旧连接断开时 run 往往仍在执行，事件不能跟着连接一起消失，否则用户只能
盯着「任务仍在后台执行」，等 run 结束才看到过程。
"""

from __future__ import annotations

import asyncio
import time

from edith_backend.internal.agentrun.run_log import (
    TRUNCATED_EVENT,
    RunEventLog,
)
from edith_backend.internal.agentrun.service import ActiveRun, Service
from edith_backend.internal.agentstream.types import Event


def _event(delta: str = "") -> Event:
    return Event(type="message.delta", request_id="r1", assistant_id="a1", delta=delta)


async def _drain(stream, limit: int = 50) -> list[Event]:
    """读到一个 None 为止（带超时，避免测试卡死）。"""
    received: list[Event] = []
    async for event in stream:
        if event is None:
            break
        received.append(event)
        if len(received) >= limit:
            break
    return received


def test_seq_is_monotonic_and_starts_at_one() -> None:
    """序号由日志统一赋值：顺序只有一个权威来源。"""
    log = RunEventLog()
    for _ in range(3):
        log.publish(_event("x"))
    assert [e.seq for e in log.backlog()] == [1, 2, 3]


def test_subscriber_that_joins_late_gets_the_backlog_then_live() -> None:
    """这是断流重连的全部价值：中途加入也能拿到之前发生的一切。"""

    async def go() -> list[Event]:
        log = RunEventLog()
        log.publish(_event("a"))
        log.publish(_event("b"))

        async def late_subscriber() -> list[Event]:
            received = []
            async for event in log.open(after=1):
                if event is None:
                    break
                received.append(event)
            return received

        task = asyncio.create_task(late_subscriber())
        await asyncio.sleep(0)  # 让它先读到补发段并挂到实时队列上
        log.publish(_event("c"))
        log.close()
        return await asyncio.wait_for(task, timeout=2)

    received = asyncio.run(go())
    # after=1：只要 1 之后的部分。补发的 b + 实时的 c，一条不多一条不少。
    assert [e.delta for e in received] == ["b", "c"]


def test_two_subscribers_both_get_every_event() -> None:
    """多个标签页/多个请求可以同时看同一个 run，互不影响。"""

    async def go() -> tuple[list[Event], list[Event]]:
        log = RunEventLog()
        first = asyncio.create_task(_drain(log.open()))
        second = asyncio.create_task(_drain(log.open(after=1)))
        await asyncio.sleep(0)
        log.publish(_event("a"))
        log.publish(_event("b"))
        log.close()
        return (
            await asyncio.wait_for(first, timeout=2),
            await asyncio.wait_for(second, timeout=2),
        )

    got_first, got_second = asyncio.run(go())
    assert [e.delta for e in got_first] == ["a", "b"]
    assert [e.delta for e in got_second] == ["b"]


def test_closed_log_can_still_be_replayed() -> None:
    """run 已经结束、前端才重连：依然要能补出完整过程。

    这就是 `_archive_log` 存在的理由——把「结束」和「日志不可读」区分开。
    """

    async def go() -> tuple[list[Event], int]:
        log = RunEventLog()
        log.publish(_event("a"))
        log.publish(_event("b"))
        log.close()
        received = await asyncio.wait_for(_drain(log.open()), timeout=2)
        # 关闭后再发的事件不进入日志：它属于「关闭之后还发事件」的编码错误，
        # 静默丢弃而不是让时间线在结束之后继续变长。
        log.publish(_event("late"))
        return received, len(log.backlog())

    received, backlog_size = asyncio.run(go())
    assert [e.delta for e in received] == ["a", "b"]
    assert backlog_size == 2


def test_truncation_is_reported_not_silent() -> None:
    """超过容量时明确告知断层，而不是让前端以为「就这么点」."""

    async def go() -> list[Event]:
        log = RunEventLog()
        log.dropped_before = 5  # 模拟已丢弃 1..5
        log.publish(_event("f"))
        log.close()
        return await asyncio.wait_for(_drain(log.open(after=0)), timeout=2)

    received = asyncio.run(go())
    assert received[0].type == TRUNCATED_EVENT
    assert [e.delta for e in received[1:]] == ["f"]


def test_eviction_keeps_memory_bounded_and_tracks_gap() -> None:
    log = RunEventLog()
    from edith_backend.internal.agentrun import run_log as module

    original = module.MAX_EVENTS
    module.MAX_EVENTS = 3
    try:
        for index in range(5):
            log.publish(_event(str(index)))
        assert len(log.backlog()) == 3
        assert log.dropped_before == 2
        # 留下来的必须是**最新**的：留着最老的等于丢掉正在发生的事实。
        assert [e.delta for e in log.backlog()] == ["2", "3", "4"]
    finally:
        module.MAX_EVENTS = original


# ── Service 层：订阅入口的准入与归档 ─────────────────────────────────────────


def _bare_service() -> Service:
    """绕开依赖装配，只测订阅这一条路径的准入逻辑。"""
    service = object.__new__(Service)
    service.active = {}
    service._archived_logs = {}
    return service


def _active(user_id: str, log: RunEventLog) -> ActiveRun:
    return ActiveRun(user_id=user_id, session_id="s1", task=None, events=log)  # type: ignore[arg-type]


def test_subscribe_returns_the_live_log_for_the_owner() -> None:
    service = _bare_service()
    log = RunEventLog()
    service.active["r1"] = _active("user-1", log)

    stream, error = service.subscribe("user-1", "r1")
    assert error is None
    assert stream is not None and stream.log is log


def test_subscribe_hides_runs_owned_by_someone_else() -> None:
    """别人的 run 一律 not_found：连「存在性」都不该泄露。"""
    service = _bare_service()
    service.active["r1"] = _active("user-1", RunEventLog())

    stream, error = service.subscribe("user-2", "r1")
    assert stream is None
    assert error is not None and error.type == "not_found"


def test_subscribe_works_after_the_run_finished() -> None:
    """run 结束后短时间内重连仍然拿得到（归档），这是「刚好在刷新时结束」的场景。"""
    service = _bare_service()
    log = RunEventLog()
    service._archive_log("user-1", "r1", log)

    stream, error = service.subscribe("user-1", "r1")
    assert error is None
    assert stream is not None and stream.log is log


def test_archived_log_expires() -> None:
    service = _bare_service()
    service._archive_log("user-1", "r1", RunEventLog())
    user_id, log, at = service._archived_logs["r1"]
    service._archived_logs["r1"] = (user_id, log, at - service.ARCHIVED_LOG_TTL - 1)

    stream, error = service.subscribe("user-1", "r1")
    assert stream is None
    assert error is not None and error.type == "not_found"
    # 过期条目应当被顺手清掉，而不是一直躺在字典里。
    assert "r1" not in service._archived_logs


def test_archive_is_bounded() -> None:
    """归档区要有上界：否则长跑服务里每个 run 都留一份日志，内存只增不减。"""
    service = _bare_service()
    # 都落在有效期内，但新旧有序：old-0 最老，newest 最新
    base = time.monotonic() - Service.ARCHIVED_LOG_TTL / 2
    for index in range(Service.MAX_ARCHIVED_LOGS):
        service._archived_logs[f"old-{index}"] = ("user-1", RunEventLog(), base + index)

    service._archive_log("user-1", "newest", RunEventLog())

    assert len(service._archived_logs) == Service.MAX_ARCHIVED_LOGS
    assert "old-0" not in service._archived_logs  # 淘汰的是最老的
    assert "newest" in service._archived_logs


# ── 路由接线 ────────────────────────────────────────────────────────────────


def test_events_endpoint_replays_backlog_and_closes() -> None:
    """重连端点必须真的把「已发生的事件」吐出来，且带上 seq。

    这是整条链路唯一容易接错的地方：日志本身对不对有单元测试，但如果路由
    没挂上或者 after 没透传，前端刷新后依然只剩一句「未连接实时输出」。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from edith_backend.internal.agentrun.types import Error, Stream
    from edith_backend.internal.http_utils import register_error_handler
    from edith_backend.internal.webadapter.adapter import Adapter

    class _StubGateway:
        def __init__(self, log: RunEventLog) -> None:
            self.log = log

        def resume(self, user_id: str, request_id: str, after: int = 0):
            if user_id != "u1" or request_id != "r1":
                return None, Error("not_found", "agent run not found")
            return Stream(log=self.log), None

    log = RunEventLog()
    log.publish(_event("a"))
    log.publish(_event("b"))
    log.close()

    app = FastAPI()
    register_error_handler(app)
    app.include_router(Adapter(_StubGateway(log)).router)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.get(
        "/internal/gateway/runs/r1/events", params={"userId": "u1", "after": 1}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.splitlines() if line.startswith("data: ")]
    # after=1 → 只补 b，且必须带 seq（前端靠它续订）。
    assert len(frames) == 1
    assert '"delta":"b"' in frames[0]
    assert '"seq":2' in frames[0]


def test_events_endpoint_hides_other_users_runs() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from edith_backend.internal.agentrun.types import Error
    from edith_backend.internal.http_utils import register_error_handler
    from edith_backend.internal.webadapter.adapter import Adapter

    class _StubGateway:
        def resume(self, user_id: str, request_id: str, after: int = 0):
            return None, Error("not_found", "agent run not found")

    app = FastAPI()
    register_error_handler(app)
    app.include_router(Adapter(_StubGateway()).router)  # type: ignore[arg-type]
    response = TestClient(app).get(
        "/internal/gateway/runs/r1/events", params={"userId": "u2", "after": 0}
    )
    assert response.status_code == 404
