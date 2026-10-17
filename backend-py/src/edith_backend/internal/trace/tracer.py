"""Tracer：一次 run 的分步打点器。

用法（业务侧几乎零侵入）：

    tracer = new_tracer(store, request_id=..., user_id=..., session_id=...)
    await tracer.begin()
    async with tracer.step(LLM_CALL, "create#1", input={"model": m}) as s:
        stream = await client.chat.completions.create(...)
        s.output = "首段文本"
        s.metadata["ttft_ms"] = 430
    await tracer.flush()
    await tracer.finish(status=OK, end_reason="completed")

三条设计约束（`docs/trace-eval-design.md` §3）：
1. 父步骤关联用 ContextVar —— 嵌套调用无需手传 parent；
   `asyncio.create_task` 会复制上下文，`asyncio.gather` 的并发子步各自独立且父链正确。
2. `seq` 在**进入**时分配（不是在退出时），这样嵌套子步一定能拿到父的 seq。
3. trace 自身绝不影响业务：所有落盘操作都吞异常只打 warning。
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from .store import Store
from .types import ERROR, Trace, TraceStep, summarize

logger = logging.getLogger(__name__)

# 单步超过该阈值即告警（探索「响应慢」的第一道网）
SLOW_STEP_MS = float(os.getenv("EDITH_TRACE_SLOW_MS", "10000"))
# 未打点时间占比超过该比例且绝对值超 200ms 时提示「这里还没埋点」
UNATTRIBUTED_WARN_RATIO = 0.3

_current_step: ContextVar[TraceStep | None] = ContextVar(
    "trace_current_step", default=None
)


def now_iso() -> str:
    """本地时区 ISO 字符串（秒级）。

    全库统一带 +HH:MM 偏移：`store.cleanup` 的字符串区间比较与
    `viewer` 的耗时换算都依赖「写入端与比较端同一口径」。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_tracer(
    store: Store | None,
    request_id: str,
    user_id: str,
    session_id: str,
    workspace_id: str | None = None,
    user_input: str = "",
) -> Tracer:
    """构造一次 run 的 Tracer。`store=None` 时全部方法空转（可作总开关）。"""
    trace = Trace(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        workspace_id=workspace_id or None,
        user_input=summarize(user_input)[0],
        started_at=now_iso(),
    )
    return Tracer(store, trace)


class Tracer:
    def __init__(self, store: Store | None, trace: Trace) -> None:
        self._store = store
        self.trace = trace
        self._seq = 0
        self._pending: list[TraceStep] = []
        self._finished = False

    @property
    def enabled(self) -> bool:
        return self._store is not None

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def begin(self) -> None:
        """写入骨架行（立即 commit：让「正在进行」立刻可见）。"""
        if not self.enabled or self._store is None:
            return
        try:
            await self._store.insert_trace(self.trace)
        except Exception as exc:  # noqa: BLE001 - trace 不影响业务
            logger.warning("trace_begin_failed request_id=%s error=%s", self.trace.request_id, exc)

    async def flush(self) -> None:
        """把缓冲中已完成的步骤批量落盘（一次 commit）。"""
        if not self.enabled or self._store is None or not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            await self._store.insert_steps(self.trace.request_id, batch)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不阻塞业务，也不重试堆积
            logger.warning(
                "trace_flush_failed request_id=%s steps=%d error=%s",
                self.trace.request_id,
                len(batch),
                exc,
            )

    async def finish(
        self,
        *,
        status: str,
        end_reason: str | None = None,
        duration_ms: int | None = None,
        total_tokens: int | None = None,
        tools_called: int | None = None,
        model_id: str | None = None,
        error: BaseException | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        traceback_text: str | None = None,
    ) -> None:
        """收尾：写终态 + 耗时分解 + 异常 + 降级，然后立刻做慢步骤体检。

        `error` 传异常对象；业务错误（非异常）可用 `error_type`/`error_message` 直接给。
        """
        if not self.enabled or self._finished or self._store is None:
            return
        self._finished = True
        t = self.trace
        t.status = status
        t.end_reason = end_reason
        t.finished_at = now_iso()
        t.duration_ms = duration_ms
        t.recompute_self_time()
        t.total_tokens = total_tokens
        t.tools_called = tools_called
        if model_id:
            t.model_id = model_id
        if error is not None:
            t.error_type = type(error).__name__
            t.error_message = str(error)[:1000]
        if error_type:
            t.error_type = error_type
        if error_message:
            t.error_message = error_message[:1000]
        if traceback_text:
            t.error_traceback = traceback_text
        try:
            await self.flush()
            await self._store.finish_trace(t)
        except Exception as exc:  # noqa: BLE001 - trace 不影响业务
            logger.warning(
                "trace_finish_failed request_id=%s error=%s", t.request_id, exc
            )
            return
        self._report(t)

    def degrade(self, kind: str, from_: str, to: str, reason: str = "") -> None:
        """记录一次降级（EDITH 的降级必须显式可见，不能静默）。"""
        entry = {"kind": kind, "from": from_, "to": to}
        if reason:
            entry["reason"] = reason
        self.trace.degraded = True
        self.trace.degradations.append(entry)
        logger.warning(
            "degraded request_id=%s kind=%s from=%s to=%s reason=%s",
            self.trace.request_id,
            kind,
            from_,
            to,
            reason or "-",
        )

    # ── 打点 ──────────────────────────────────────────────────────────

    @asynccontextmanager
    async def step(
        self,
        type_: str,
        name: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ):
        """包一层：自动计时、自动父子关联、自动捕获异常（记录后继续抛）。

        `input` 与 `s.output` 只存摘要（默认 digest；`EDITH_TRACE_FULL=1` 存原文）。
        """
        if not self.enabled:
            yield TraceStep(type=type_, name=name)
            return

        self._seq += 1
        parent = _current_step.get()
        s = TraceStep(
            type=type_,
            name=name,
            seq=self._seq,
            parent_seq=parent.seq if parent is not None else None,
            depth=(parent.depth + 1) if parent is not None else 0,
            started_at=now_iso(),
        )
        if metadata:
            s.metadata.update(metadata)
        if input is not None:
            s.input_digest, s.input_len = summarize(input)

        token = _current_step.set(s)
        started = time.monotonic()
        try:
            yield s
        except Exception as exc:
            s.status = ERROR
            s.error_type = type(exc).__name__
            s.error_message = str(exc)[:1000]
            raise
        finally:
            s.duration_ms = round((time.monotonic() - started) * 1000, 2)
            _current_step.reset(token)
            if s.output is not None:
                s.output_digest, s.output_len = summarize(s.output)
            self.trace.steps.append(s)
            self._pending.append(s)

    def note(self, step: TraceStep, output: Any) -> None:
        """给已结束/进行中的 step 补输出摘要（便于在 with 块外记录）。"""
        step.output_digest, step.output_len = summarize(output)

    # ── 体检（跑完立刻报，比事后查高效） ────────────────────────────────

    def _report(self, t: Trace) -> None:
        try:
            slow = [s for s in t.steps if s.duration_ms >= SLOW_STEP_MS]
            if slow:
                detail = ", ".join(
                    f"{s.name} {s.duration_ms:.0f}ms[{s.type}]" for s in slow[:5]
                )
                logger.warning(
                    "trace_slow request_id=%s status=%s count=%d slow=[%s]",
                    t.request_id,
                    t.status,
                    len(slow),
                    detail,
                )
            unattr = t.unattributed_ms()
            if (
                unattr is not None
                and t.duration_ms
                and unattr > 200
                and unattr / t.duration_ms > UNATTRIBUTED_WARN_RATIO
            ):
                logger.warning(
                    "trace_unattributed request_id=%s wall_ms=%d self_ms=%d unaccounted_ms=%d",
                    t.request_id,
                    t.duration_ms,
                    t.self_time_ms or 0,
                    unattr,
                )
            logger.info(
                "trace end request_id=%s status=%s end_reason=%s wall_ms=%s self_ms=%s "
                "unaccounted_ms=%s steps=%d tools=%s tokens=%s degraded=%s",
                t.request_id,
                t.status,
                t.end_reason,
                t.duration_ms,
                t.self_time_ms,
                unattr,
                len(t.steps),
                t.tools_called,
                t.total_tokens,
                t.degraded,
            )
        except Exception as exc:  # noqa: BLE001 - 体检失败不影响业务
            logger.warning("trace_report_failed error=%s", exc)


def format_traceback(exc: BaseException | None = None) -> str:
    """取当前异常（或指定异常）的堆栈文本，供 trace 落库。"""
    if exc is not None:
        return "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[:8000]
    return traceback.format_exc()[:8000]


__all__ = ["Tracer", "format_traceback", "new_tracer", "now_iso"]
