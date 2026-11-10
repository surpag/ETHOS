"""agentrun 服务：EDITH 唯一的 Agent 执行入口。

对应 Go 版 internal/agentrun/service.go。
Run 主流程：规范化 → 校验 → 会话准入 → 配置聚合 → 记录用量 → 启动流式执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from ..agentstream.outcome import derive_outcome
from ..agentstream.types import APIError, Event, SessionUsage
from ..models.catalog import DEFAULT_MODEL_ID, Catalog, ModelDefinition
from ..session.constraint_store import ConstraintStore, constraints_enabled
from ..session.plan_store import RunPlanStore
from ..session.store import SessionStore, WorkspaceSessionConflict
from ..skills.catalog import Catalog as SkillsCatalog
from ..skills.storage import SkillScopeStores
from ..trace import (
    CANCELED,
    DEGRADE_MCP,
    DEGRADE_MODEL,
    ERROR,
    MCP,
    MEMORY,
    OK,
    PREPARE,
    RESUME,
    Tracer,
    new_tracer,
)
from ..trace import format_traceback as _format_traceback
from ..trace.store import Store as TraceStore
from ..usage.recorder import Recorder, RunAlreadyExists
from ..usage.types import Run, Summary, Tokens
from ..userconfig.providers import Providers
from ..userconfig.settings import Settings
from .configuration import ConfiguredRun, RunConfigurations
from .execution import (
    ToolCircuitBreakerError,
    chat,
    elapsed_ms,
    tokens_from_usage,
)
from .progress import ProgressRecorder
from .run_log import RunEventLog
from .run_plan import RunPlanRegistry, render_plan_block, to_snapshot
from .session_lanes import SessionLanes
from .types import Error, Request, Status, Stream
from .user_stops import UserStops
from .workspace_lanes import WorkspaceLanes

# 新增
# from ..userconfig.mcp_runtime import MCPRuntime

logger = logging.getLogger(__name__)


@dataclass
class Dependencies:
    """AgentRun 聚合一次执行所需的长期能力。"""

    models: Catalog
    settings: Settings
    providers: Providers
    store: SessionStore
    usage: Recorder
    skills: SkillsCatalog | None = None
    # 作用域分发点：解析器要读项目级 Skill 时按 workspace_id 取门面。
    # 不传时项目层按「未配置」上报（不是报错）——纯聊天会话本来就没有项目级。
    skill_scopes: SkillScopeStores | None = None
    images: object | None = None
    files: object | None = None
    mcp: object | None = None
    memories: object | None = None
    constraints: ConstraintStore | None = None
    # 活跃计划注册表：plan_update 工具与 run 收尾共用同一份（单一权威源）
    plans: RunPlanRegistry | None = None
    # 计划快照表：续跑时从这里取「上次排到哪一步」（注册表在 run 结束时已清空）
    plan_store: RunPlanStore | None = None
    tool_definitions: list[dict] = field(default_factory=list)
    tool_executor: object | None = None
    traces: TraceStore | None = None
    # run 收尾兜底：把本轮沙箱里最后一次未回拉的用户 Skill 写入落地。
    # 放在这里而不是写死在 agentrun 内部，是为了不让「调度层」认识 sandbox/skills。
    skill_flush: Callable[[str, str], Awaitable[None]] | None = None


@dataclass
class ActiveRun:
    """一个仍在执行的活跃任务。"""

    user_id: str
    session_id: str
    task: asyncio.Task
    # 这个 run 的事件日志。保留在这里不只是为了「它的输出」——run 结束后
    # 还要放一段时间供断流重连补发（见 _archive_log）。
    events: RunEventLog


class Service:
    def __init__(self, deps: Dependencies) -> None:
        self.configurations = RunConfigurations(
            deps.models,
            deps.settings,
            deps.providers,
            deps.skills,
            deps.images,
            deps.files,
            deps.mcp,
            deps.memories,
            deps.constraints,
            deps.skill_scopes,
        )
        self.store = deps.store
        self.constraints = deps.constraints
        self.plans = deps.plans
        self.plan_store = deps.plan_store
        self.usage = deps.usage
        self.tool_definitions = deps.tool_definitions
        self.tool_executor = deps.tool_executor
        self.lanes = SessionLanes()
        # 工作区互斥租约：同一 workspace 同一时刻只允许一个 session 使用（正确性要求）
        self.workspace_lanes = WorkspaceLanes()
        self.user_stops = UserStops()
        self.active: dict[str, ActiveRun] = {}
        # run 结束后事件日志的暂存区：request_id → (用户, 日志, 归档时刻)。
        # 用途只有一个——用户刷新页面/断流时，run 可能刚好在这几秒内结束；
        # 若不暂存，重连会拿到 404，只能退回「拉历史」，中间过程依然看不到。
        self._archived_logs: dict[str, tuple[str, RunEventLog, float]] = {}
        # 新增
        self.mcp = deps.mcp
        self.models = deps.models
        self.settings = deps.settings
        self.providers = deps.providers
        self.memories = deps.memories
        # trace 落盘器；为 None 时打点全部空转（可作总开关）
        self.traces = deps.traces
        self.skill_flush = deps.skill_flush

    async def run(self, request: Request) -> tuple[Stream | None, Error | None]:
        """按「会话准入 → 配置聚合 → 记录用量 → 启动流式执行」启动一次任务。"""
        request = _normalize_request(request)
        if err := _validate_request(request):
            return None, err

        # 一次 run 一条 trace：从准入开始，任何早退分支都收成失败态，不留悬空的 running
        tracer = new_tracer(
            self.traces,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
            workspace_id=request.workspace_id,
            user_input=request.message,
        )
        await tracer.begin()

        async def _abort(error: Error) -> tuple[None, Error]:
            await tracer.finish(
                status=ERROR,
                end_reason=error.type,
                error_type=error.type,
                error_message=error.message,
            )
            return None, error

        if not self.lanes.acquire(
            request.user_id, request.session_id, request.request_id
        ):
            return await _abort(
                Error(
                    "session_busy", "an agent run is already active for this session"
                )
            )
        # 工作区互斥租约：同一工作区同一时刻只允许一个会话使用（防止两个沙箱互相覆盖镜像）
        if request.workspace_id and not self.workspace_lanes.acquire(
            request.user_id, request.workspace_id, request.session_id
        ):
            self.lanes.release(request.user_id, request.session_id, request.request_id)
            holder = self.workspace_lanes.holder(request.user_id, request.workspace_id)
            return await _abort(
                Error(
                    "workspace_busy",
                    "this workspace is being used by another session"
                    + (f" ({holder})" if holder else ""),
                )
            )
        logger.info(
            "agentrun start request_id=%s user=%s session=%s model=%s",
            request.request_id,
            request.user_id,
            request.session_id,
            request.model_id,
        )

        # 抽取本轮用户消息里的约束——**必须在 load_configuration 之前**。
        # 约束要在本轮 system prompt 里就生效；放在消息落库之后（= 配置已加载完）
        # 会让「用户刚说完必须用 Python」这一轮反而注入不到，模型先违反一次才被纠正。
        # 溯源 id 落库后再回填（此时消息还没写库）。
        # 抽取失败不能拖垮主流程（它只是增强），但必须留下痕迹。
        pending_constraint_ids: list[int] = []
        if self.constraints is not None and constraints_enabled():
            try:
                pending_constraint_ids = await self.constraints.capture_message(
                    request.user_id,
                    session_id=request.session_id,
                    workspace_id=request.workspace_id or None,
                    message=request.message,
                )
                if pending_constraint_ids:
                    logger.info(
                        "constraints_captured user=%s session=%s count=%d ids=%s",
                        request.user_id,
                        request.session_id,
                        len(pending_constraint_ids),
                        pending_constraint_ids,
                    )
            except Exception:
                logger.exception(
                    "constraints_capture_failed user=%s session=%s",
                    request.user_id,
                    request.session_id,
                )

        # 准备阶段（曾经是黑盒）：模型/密钥/人格/Skill/记忆/图片都在这里加载
        async with tracer.step(PREPARE, "load_configuration") as s:
            configured = await self.configurations.load(request, tracer)
            if isinstance(configured, Error):
                s.status = ERROR
                s.error_type = configured.type
                s.error_message = configured.message
            else:
                s.metadata["model"] = configured.definition.id
                tracer.trace.model_id = configured.definition.id
        if isinstance(configured, Error):
            self.lanes.release(request.user_id, request.session_id, request.request_id)
            self.workspace_lanes.release(
                request.user_id, request.workspace_id, request.session_id
            )
            return await _abort(configured)

        run = Run(
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
            model_id=request.model_id,
        )
        try:
            await self.usage.start(run)
        except RunAlreadyExists:
            self.lanes.release(request.user_id, request.session_id, request.request_id)
            self.workspace_lanes.release(
                request.user_id, request.workspace_id, request.session_id
            )
            return await _abort(Error("request_conflict", "agent run already exists"))

        # 持久化用户消息，供会话历史回放（带工作区归属，NULL = 普通对话）
        message_id = 0
        try:
            message_id = await self.store.add_user_message(
                request.user_id,
                request.session_id,
                request.message,
                request.image_ids,
                request.workspace_id or None,
            )
        except WorkspaceSessionConflict as exc:
            # 工作区已被另一个会话占用（一个工作区一个会话）。这里必须显式返回业务
            # 错误而不是让异常冒到 500：前端要据此切到那个会话，而不是重试。
            self.lanes.release(request.user_id, request.session_id, request.request_id)
            self.workspace_lanes.release(
                request.user_id, request.workspace_id, request.session_id
            )
            return await _abort(
                Error(
                    "workspace_session_conflict",
                    "this workspace already has an active session"
                    + (f": {exc.existing_session_id}" if exc.existing_session_id else ""),
                )
            )

        # 抽取本轮用户消息里的约束（历史之外的一层，永不参与压缩）。
        # 回填溯源 id：约束行的 source_message_id 让「这条约束是谁说的」可回查。
        if pending_constraint_ids and message_id:
            try:
                await self.constraints.link_source(
                    request.user_id, pending_constraint_ids, message_id
                )
            except Exception:
                logger.exception(
                    "constraints_link_source_failed user=%s session=%s",
                    request.user_id,
                    request.session_id,
                )

        events = RunEventLog(request_id=request.request_id)
        task = asyncio.create_task(self._run(configured, run, events, tracer))
        self.active[request.request_id] = ActiveRun(
            user_id=request.user_id,
            session_id=request.session_id,
            task=task,
            events=events,
        )
        return Stream(log=events), None

    def status(
        self, user_id: str, request_id: str
    ) -> tuple[Status | None, Error | None]:
        """查询属于该用户的活跃任务。"""
        user_id = user_id.strip()
        request_id = request_id.strip()
        if not user_id or not request_id:
            return None, Error("invalid_request", "userId and requestId are required")
        run = self.active.get(request_id)
        if run is None or run.user_id != user_id:
            return None, Error("not_found", "agent run not found")
        return Status(request_id=request_id, status="running"), None

    def subscribe(
        self, user_id: str, request_id: str, after: int = 0
    ) -> tuple[Stream | None, Error | None]:
        """重新订阅一个 run 的事件流（断流重连的唯一入口）。

        与 `run()` 的区别：它不启动任何东西，只回放。这正是刷新页面后
        「立刻看到已经发生的执行过程」所需要的——已经发生的事实一直在日志里，
        旧连接断了不等于它就消失了。

        归属校验与 status/cancel 一致：别人的 run 一律 not_found，不泄露存在性。
        找不到（从未存在 / 已归档过期 / 进程重启过）时返回 not_found，调用方
        自行降级为「拉历史」——那是唯一诚实的退路。
        """
        user_id = user_id.strip()
        request_id = request_id.strip()
        if not user_id or not request_id:
            return None, Error("invalid_request", "userId and requestId are required")

        run = self.active.get(request_id)
        if run is not None:
            if run.user_id != user_id:
                return None, Error("not_found", "agent run not found")
            return Stream(log=run.events), None

        archived = self._take_archived(request_id)
        if archived is None or archived[0] != user_id:
            return None, Error("not_found", "agent run not found")
        return Stream(log=archived[1]), None

    # 归档保留时长：足够覆盖「刷新 + 重新加载页面」的几秒，又不至于长期占内存。
    ARCHIVED_LOG_TTL = 600.0
    MAX_ARCHIVED_LOGS = 32

    def _archive_log(self, user_id: str, request_id: str, events: RunEventLog) -> None:
        """run 收尾后把日志挪进暂存区，供短时间内重连补发。"""
        now = time.monotonic()
        self._archived_logs[request_id] = (user_id, events, now)
        # 惰性清理：只在写入时扫，不需要后台任务
        expired = [
            key
            for key, (_, _, at) in self._archived_logs.items()
            if now - at > self.ARCHIVED_LOG_TTL
        ]
        for key in expired:
            self._archived_logs.pop(key, None)
        while len(self._archived_logs) > self.MAX_ARCHIVED_LOGS:
            oldest = min(self._archived_logs.items(), key=lambda item: item[1][2])[0]
            self._archived_logs.pop(oldest, None)

    def _take_archived(
        self, request_id: str
    ) -> tuple[str, RunEventLog] | None:
        """取暂存日志（不删除：同一个 run 可能被多个标签页/多次刷新重连）。"""
        entry = self._archived_logs.get(request_id)
        if entry is None:
            return None
        user_id, events, at = entry
        if time.monotonic() - at > self.ARCHIVED_LOG_TTL:
            self._archived_logs.pop(request_id, None)
            return None
        return user_id, events

    def cancel(self, user_id: str, request_id: str) -> Error | None:
        """校验任务归属后取消任务，并标记用户主动停止。"""
        user_id = user_id.strip()
        request_id = request_id.strip()
        if not user_id or not request_id:
            return Error("invalid_request", "userId and requestId are required")
        run = self.active.get(request_id)
        if run is None or run.user_id != user_id:
            return Error("not_found", "agent run not found")
        self.user_stops.mark(request_id)
        run.task.cancel()
        return None

    # 标记事件开始，取消，结束，记录过程中的token消耗。在关闭事件前先关闭mcp连接
    async def _run(
        self,
        configured: ConfiguredRun,
        run: Run,
        events: RunEventLog,
        tracer: Tracer,
    ) -> None:
        """流式执行的完整生命周期：run.started → 解码事件 → 终止事件 → 收尾。"""
        request = configured.request
        assistant_id = f"assistant_{request.request_id}"
        mcp_tools = None
        start_time = time.monotonic()
        reason = "completed"
        tokens = None  # 供 finally 使用（异常路径可能未赋值）
        end_reason = None  # chat() 的终止原因（诊断用；恢复依据是 task_status）
        tool_calls_count = 0
        mcp_tools_count = 0
        used_model_id = configured.definition.id
        # trace 收尾依据：默认按失败，只有走到正常出口才改成 ok/canceled
        trace_status = ERROR
        trace_error: BaseException | None = None

        try:
            events.publish(
                Event(
                    type="run.started",
                    session_id=request.session_id,
                    request_id=request.request_id,
                    assistant_id=assistant_id,
                )
            )

            # 任务状态：run 启动即标记 in_progress（后续只有"模型主动完成/用户取消"会改走，
            # 其余异常终止一律不改——因此崩溃后状态天然停留在 in_progress，可被恢复）
            await self.store.set_task_status(
                request.user_id, request.session_id, "in_progress"
            )

            if self.mcp is not None:
                async with tracer.step(MCP, "open_tools") as mcp_step:
                    mcp_tools = await self.mcp.open_tools(request.user_id)
                    mcp_tools_count = len(mcp_tools.tool_definitions())
                    mcp_step.metadata["tools"] = mcp_tools_count
                    # 连不上的服务必须让这一步变色：以前它们只留一行 warning，
                    # 追踪里依旧全绿，于是「MCP 鉴权 401 全挂」看起来和
                    # 「这个用户没配 MCP」一模一样。
                    failures = getattr(mcp_tools, "failures", None) or []
                    if failures:
                        mcp_step.metadata["failed"] = len(failures)
                        mcp_step.metadata["failed_servers"] = " | ".join(
                            f"{item.get('server', '?')}: {item.get('error', '')}"
                            for item in failures[:5]
                        )
                        tracer.degrade(
                            DEGRADE_MCP,
                            f"{mcp_tools_count}_tools",
                            "none" if mcp_tools_count == 0 else "partial",
                            reason=f"{len(failures)} 个服务连接失败",
                        )
                        if mcp_tools_count == 0:
                            mcp_step.status = ERROR
            fallback_models = await self._build_fallback_models(
                request.user_id, configured.definition.id, tracer
            )
            # 断点续跑：只看 task_status + checkpoint（与终止原因解耦，无 end_reason 特例判断）
            await self._inject_resume_context(configured, tracer)

            # 运行中的进度落库：让「刷新页面」看到的是已发生的执行过程，
            # 而不是只有用户那一句。终态写入会覆盖它（同一条行、同一个 request_id）。
            progress = ProgressRecorder(
                self.store,
                user_id=request.user_id,
                session_id=request.session_id,
                request_id=request.request_id,
                workspace_id=request.workspace_id or None,
            )
            (
                tokens,
                used_model_id,
                tool_calls_count,
                end_reason,
                duration,
                completion_evidence,
            ) = await chat(
                configured,
                self.store,
                events,
                assistant_id,
                tools=self.tool_definitions,
                tool_executor=self.tool_executor,
                mcp_tools=mcp_tools,
                fallback_models=fallback_models,
                started_at=start_time,
                tracer=tracer,
                plans=self.plans,
                progress=progress,
            )
            # 任务状态：仅"模型主动完成"写 completed 并清进度；
            # rounds_exhausted 等未完成终止保持 in_progress（供下次续跑）
            if end_reason == "completed":
                await self.store.set_task_status(
                    request.user_id, request.session_id, "completed"
                )
                await self.store.clear_checkpoints(request.user_id, request.session_id)
            summary = await self.usage.finish(run, tokens)
            event_type = "run.completed"
            if self.user_stops.take(request.request_id):
                event_type = "run.canceled"
            reason = event_type
            trace_status = CANCELED if event_type == "run.canceled" else OK
            fallback_mark = None
            if used_model_id != configured.definition.id:
                fallback_mark = f"{configured.definition.id}→{used_model_id}"
                tracer.degrade(
                    DEGRADE_MODEL,
                    configured.definition.id,
                    used_model_id,
                    end_reason or "",
                )
            # 最终计划快照：与 plan.updated 事件同源（同一个 RunPlan 对象），
            # 让前端在终态事件里也能对齐到最后一版，不必依赖事件是否丢包。
            final_plan = (
                to_snapshot(self.plans.current(request.user_id, request.session_id))
                if self.plans is not None
                else None
            )
            events.publish(
                Event(
                    type=event_type,
                    request_id=request.request_id,
                    session_id=request.session_id,
                    assistant_id=assistant_id,
                    usage=_summary_to_session_usage(summary),
                    fallback=fallback_mark,
                    end_reason=end_reason,  # 让 UI 区分"跑完了"vs"轮次耗尽未完成"
                    # 语义投影：UI 只判「完成/未完成」，不必枚举 end_reason。
                    # 与落库值同源（同一个 derive_outcome），刷新前后一致。
                    outcome=derive_outcome(end_reason),
                    # 与落库值同源（chat 返回），保证 UI 实时显示与刷新历史完全一致
                    duration_ms=duration,
                    # 「模型结束了」≠「已核实有产出」：UI 靠这个字段把两者分开显示
                    completion_evidence=completion_evidence,
                    plan=final_plan,
                )
            )
        except asyncio.CancelledError as exc:
            reason = "canceled"
            trace_status = CANCELED
            trace_error = exc
            logger.info(
                "agentrun canceled request_id=%s elapsed_ms=%d",
                request.request_id,
                elapsed_ms(start_time),
            )
            # 用户主动取消：标记 canceled 并清进度（尊重用户意图，下次不自动续跑）
            await self.store.set_task_status(
                request.user_id, request.session_id, "canceled"
            )
            await self.store.clear_checkpoints(request.user_id, request.session_id)
            await self.usage.fail(request.request_id)
            events.publish(
                Event(
                    type="run.canceled",
                    request_id=request.request_id,
                    session_id=request.session_id,
                    assistant_id=assistant_id,
                    duration_ms=elapsed_ms(start_time),
                )
            )
            raise
        except ToolCircuitBreakerError as exc:
            # 工具连续系统故障熔断：明确报错，而非空转到 completed
            reason = "error"
            trace_error = exc
            logger.error(
                "agentrun tool_circuit_breaker request_id=%s tool=%s failures=%d error=%s",
                request.request_id,
                exc.tool_name,
                exc.failures,
                exc.last_error[:200],
            )
            await self.usage.fail(request.request_id)
            events.publish(
                Event(
                    type="run.error",
                    request_id=request.request_id,
                    session_id=request.session_id,
                    assistant_id=assistant_id,
                    duration_ms=elapsed_ms(start_time),
                    error=APIError(type="tool_circuit_breaker", message=str(exc)),
                )
            )
        except Exception as exc:  # task 顶层须捕获任意异常转为 run.error
            reason = "error"
            trace_error = exc
            # 修复可观测性缺口：此前只发 run.error 事件、不留日志，异常堆栈永久丢失
            logger.exception("agentrun failed request_id=%s", request.request_id)
            await self.usage.fail(request.request_id)
            events.publish(
                Event(
                    type="run.error",
                    request_id=request.request_id,
                    session_id=request.session_id,
                    assistant_id=assistant_id,
                    duration_ms=elapsed_ms(start_time),
                    error=APIError(type="internal_error", message=str(exc)),
                )
            )
        finally:
            if mcp_tools is not None:
                await mcp_tools.close()
            # 用户 Skill 兜底回拉：本轮若有用「工具钩子判断不出」的方式改过 Skill
            # （如 `python build_skills.py`），在这里落地；否则下一轮索引里看不到它。
            # 放在收尾最前面：它读的是沙箱现状，越早越接近「本轮结束时」。
            if self.skill_flush is not None:
                try:
                    await self.skill_flush(request.user_id, request.session_id)
                except Exception:
                    logger.warning("flush user skills on run end failed", exc_info=True)
            # 计划生命周期跟着 run 走：无论正常收尾、取消还是异常，都要关掉，
            # 否则注册表里会留下一条「活跃计划」，下次同会话的 run 只能覆盖它。
            if self.plans is not None:
                self.plans.close(request.request_id)
            self.lanes.release(request.user_id, request.session_id, request.request_id)
            self.workspace_lanes.release(
                request.user_id, request.workspace_id, request.session_id
            )
            # 先归档日志、再摘掉活跃标记：反过来的话，中间那一瞬重连会 404。
            # 归档后日志仍可继续接收收尾事件（如 usage.updated），直到 close()。
            self._archive_log(request.user_id, request.request_id, events)
            self.active.pop(request.request_id, None)
            self.user_stops.take(request.request_id)

            # 调用一次模型，自动记忆写入（它本身是一次真实模型调用，必须计入用量）。
            #
            # 顺序说明：这一步放在 `None` 哨兵**之前**，才有机会把「补记后的用量」
            # 作为事件推给前端。否则哨兵一发，流就结束了，前端只能等下次刷新才看
            # 到真实数字——那正是「实时值与历史值不同源」的老问题。
            # 哨兵本身放在 finally：它是「流结束」的唯一信号，缺了它 SSE 连接会
            # 一直挂着（前端只能等网络层超时），所以无论上面发生什么都不许漏发。
            try:
                await self._extract_memories_on_finish(
                    configured, run, tokens, reason, events, tracer
                )
            finally:
                events.close()

            duration_ms = elapsed_ms(start_time)
            # trace 收尾：终态 + 耗时分解（墙钟 / 自耗时 / 未打点）+ 异常堆栈 + 降级
            await tracer.finish(
                status=trace_status,
                end_reason=end_reason,
                duration_ms=duration_ms,
                total_tokens=tokens.total_tokens if tokens else None,
                tools_called=tool_calls_count,
                model_id=used_model_id,
                error=trace_error,
                traceback_text=(
                    _format_traceback(trace_error)
                    if trace_error is not None and trace_status == ERROR
                    else None
                ),
            )
            logger.info(
                "agentrun end request_id=%s duration_ms=%.2f tokens=%d mcp_tools=%d tools_called=%d reason=%s end_reason=%s",
                request.request_id,
                duration_ms,
                tokens.total_tokens if tokens else 0,
                mcp_tools_count,  # 连接的工具数（已有）
                tool_calls_count,  # 本次实际调用的次数（新增）
                reason,
                end_reason,  # 终止诊断（与恢复依据 task_status 解耦）
            )

    async def _inject_resume_context(
        self, configured: ConfiguredRun, tracer: Tracer
    ) -> None:
        """断点续跑：只看 task_status + checkpoint，与终止原因（end_reason）无关。

        上次任务处于 in_progress 且有进度记录时，注入续接提示，让模型接着做而非重扫。
        """
        request = configured.request
        try:
            status = await self.store.get_task_status(request.user_id, request.session_id)
            if status != "in_progress":
                return
            ckpt = await self.store.load_latest_checkpoint(
                request.user_id, request.session_id
            )
            if not ckpt:
                return
            # 只在真的发生续跑时打点（否则每次 run 都会多一条无意义的 resume 步骤）
            async with tracer.step(RESUME, "inject_context") as s:
                s.metadata["step"] = ckpt["step"]
                s.metadata["last_tool"] = ckpt["tool"]
                note = (
                    "\n\n【续接提示】上一次任务在未完成的状态下中断（已执行 "
                    f"{ckpt['step']} 步工具调用，最后一步：{ckpt['tool']}）。"
                    "请基于已有上下文继续完成剩余部分，不要从头重复已完成的工作。"
                )
                # 已读文件摘要（read_checkpoint）：按 path 聚合，避免重复扫描已读文件
                reads = await self.store.load_read_progress(
                    request.user_id, request.session_id
                )
                if reads:
                    seen: dict[str, dict] = {}
                    for r in reads:
                        path = r.get("path", "")
                        if not path:
                            continue
                        prev = seen.get(path)
                        if prev is None or (r.get("endLine") or 0) > (
                            prev.get("endLine") or 0
                        ):
                            seen[path] = r
                    items = list(seen.values())[-20:]  # 最多列 20 个文件
                    s.metadata["read_files"] = len(items)
                    lines = [
                        f"- {r.get('path')}（已读 {r.get('startLine')}-"
                        f"{r.get('endLine')}/{r.get('totalLines')} 行）"
                        for r in items
                    ]
                    note += "\n已读文件（不必重读，除非需要确认）：\n" + "\n".join(lines)
                # 上次的计划：续跑最容易犯的错就是「重新规划 + 重复已完成的工作」。
                # 计划是状态不是历史，此刻历史里可能只剩一条被压缩过的摘要，
                # 所以要从快照表原文重放。放在 system **末尾**——开头改一个字
                # 就会让整段前缀缓存归零（实测 4 字 → 命中率 0）。
                plan_block = await self._render_resume_plan(
                    request.user_id, request.session_id
                )
                if plan_block:
                    note += "\n\n" + plan_block
                    s.metadata["plan_restored"] = True
                configured.system_instruction += note
        except Exception as exc:  # noqa: BLE001 - 恢复上下文失败不阻塞主流程
            logger.warning("inject resume context failed: %s", exc)

    async def _render_resume_plan(self, user_id: str, session_id: str) -> str:
        """上次中断时的计划（没有就返回空串）。

        为什么不用注册表：`RunPlanRegistry` 在 run 收尾时会被 close，续跑发生时
        它一定是空的。计划能跨 run 存在的唯一理由是有人把它落了库——所以这里
        读的是快照表，与前端刷新后看到的是同一份数据。
        """
        if self.plan_store is None:
            return ""
        try:
            snapshot = await self.plan_store.latest(user_id, session_id)
        except Exception as exc:  # noqa: BLE001 - 读不到计划不该挡住续跑
            logger.warning("load plan snapshot failed: %s", exc)
            return ""
        items = (snapshot or {}).get("items") or []
        if not items:
            return ""
        return render_plan_block(
            items,
            header="【上次的计划】",
            note=(
                "（这是上次中断时的状态。请从「进行中 / 待处理」的项继续，"
                "不要重复已完成的部分；每推进一步用 plan_update 更新，"
                "标 done 时补上能核对的证据）"
            ),
        )

    @staticmethod
    def _build_summary_prompt(conversation: list[dict[str, str]]) -> str:
        """构造提取记忆的 Prompt，强制 JSON 输出。"""
        # 把对话转成文本
        dialog_text = "\n".join(
            f"{m['role']}: {m['content']}" for m in conversation if m.get("content")
        )

        return f"""你是一个对话总结助手。请从以下对话中提取 1-3 条关于该项目的**稳定事实**或**用户偏好**。

规则：
1. 只提取"用户明确说过的、可重复使用的事实"，不要推测。
2. 每条不超过 50 个字（中文）。
3. 如果对话中没有值得记忆的新信息，返回空列表。
4. 必须返回合法的 JSON 数组，格式严格如下：

示例输出：
["后端使用 FastAPI 框架", "数据库是 SQLite，端口 8080", "用户偏好使用 uv 而非 pip"]

对话内容：
{dialog_text}

现在只输出 JSON 数组，不要其他内容："""

    async def _extract_memories_on_finish(
        self,
        configured: ConfiguredRun,
        run: Run,
        tokens: Tokens | None,
        reason: str,
        events: RunEventLog,
        tracer: Tracer,
    ) -> None:
        """收尾时抽取工作区记忆，并把这次调用计入用量。

        为什么不是「附带做一下」：它是一次完整的模型调用（把最近 20 条对话塞进
        prompt），和摘要压缩同级别。以前它调用完就把 usage 丢了，成本在账本上
        根本不存在——用量面板永远显示"只有主对话"，而账单里多了一笔。
        现在补记在同一次运行上，并推 `usage.updated` 让前端拿到刷新后的汇总。
        """
        request = configured.request
        if reason not in ("completed", "canceled"):
            return
        if not request.workspace_id or tokens is None:
            return
        try:
            aux_tokens: Tokens | None = None
            async with tracer.step(MEMORY, "extract_workspace_memories") as step:
                # 目前只能和浏览器共享一个模型
                client = AsyncOpenAI(
                    api_key=configured.api_key,
                    base_url=configured.definition.base_url,
                    max_retries=2,
                )
                history = await self.store.get_messages(
                    request.user_id, request.session_id
                )
                # 过滤 system 和 tool，只保留 user/assistant 对话
                filtered = [
                    m
                    for m in history
                    if m["role"] in ("user", "assistant") and m.get("content")
                ]
                if filtered:
                    aux_tokens = await self._extract_and_save_memories(
                        configured=configured,
                        conversation=filtered[-20:],  # 最多取最近 20 条
                        workspace_id=request.workspace_id,
                        user_id=request.user_id,
                        client=client,
                    )
                if aux_tokens is not None:
                    step.metadata["tokens"] = aux_tokens.total_tokens
                    step.metadata["prompt_tokens"] = aux_tokens.prompt_tokens
                else:
                    # 没上报 = 这笔支出没有计入账本。必须显式可见，
                    # 否则「用量永远对不上」会变成一种无法归因的常态。
                    step.metadata["tokens_unreported"] = True
            if aux_tokens is not None:
                refreshed = await self.usage.add_auxiliary(run, aux_tokens)
                events.publish(
                    Event(
                        type="usage.updated",
                        request_id=request.request_id,
                        session_id=request.session_id,
                        usage=_summary_to_session_usage(refreshed),
                    )
                )
        except Exception as exc:  # 记忆抽取失败不影响运行结果
            logger.warning("auto_memory failed: %s", exc, exc_info=True)

    # 后期优化：采用结构化摘要记忆，更加准确
    async def _extract_and_save_memories(
        self,
        configured: ConfiguredRun,
        conversation: list[dict[str, str]],
        workspace_id: str,
        user_id: str,
        client: AsyncOpenAI,
    ) -> Tokens | None:
        """调用一次模型，从对话中提取稳定事实/偏好，写入自动记忆（kind='auto'）。

        返回这次调用的 token 用量（供应商没上报时返回 None）。

        为什么一定要返回用量：这是**一次真实的模型调用**，和摘要压缩一样会产生
        成本。以前它调用完就丢，成本在账本上根本不存在——"每次对话都多花一笔钱，
        但用量面板永远是 1 次调用"。附属调用也要有账。
        """
        if not conversation:
            return None

        # 1. 构造 Prompt
        prompt = self._build_summary_prompt(conversation)

        try:
            response = await client.chat.completions.create(
                model=configured.definition.api_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,  # 限制摘要长度，防止过长
            )
            raw = response.choices[0].message.content or ""
            tokens = tokens_from_usage(getattr(response, "usage", None))
            if not tokens.total_tokens:
                # 供应商没上报：不掺估算进账本，但要留下痕迹（否则又是一笔无账支出）
                tokens = None

            # 2. 解析 JSON（带兜底）
            memories = _parse_memory_json(raw)

            if not memories:
                logger.debug("auto_memory: no new facts extracted")
                return tokens

            # 3. 逐条写入自动记忆（UNIQUE 防重，幂等）
            for content in memories:
                if len(content) > 50:
                    content = content[:50]
                await self.memories.add(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    content=content,
                    kind="auto",  # 标识为自动记忆
                )

            logger.info("auto_memory: saved %d memories", len(memories))
            return tokens

        except Exception as exc:
            logger.warning("auto_memory extraction failed: %s", exc, exc_info=True)
            return None

    async def _build_fallback_models(
        self, user_id: str, current_model_id: str, tracer: Tracer | None = None
    ) -> list[tuple[ModelDefinition, str]]:
        """构建模型回退候选：用户默认模型 → 平台默认模型。

        过滤掉当前模型、目录中不存在的模型、以及未配置 API Key 的供应商。

        为什么要把「跳过原因」打进 trace：
            一次 402 事故里，回退链的候选被逐个静默 `continue` 掉了，现场只剩
            一行「trying next」的日志——看起来是在不停重试，实际候选是空的。
            「没有候选」和「有候选但都失败」是完全不同的两种故障，前者要改配置，
            后者要改模型，混淆在一起就只能靠猜。所以这里不仅返回候选，还要把
            被跳过的每个模型和原因写下来。
        """
        candidates: list[str] = []
        user_default = await self.settings.load_default_model_id(user_id)
        for model_id in (user_default, DEFAULT_MODEL_ID):
            if model_id and model_id != current_model_id and model_id not in candidates:
                candidates.append(model_id)
        result: list[tuple[ModelDefinition, str]] = []
        skipped: list[str] = []
        for model_id in candidates:
            definition = await self.models.find_for(user_id, model_id)
            if definition is None:
                skipped.append(f"{model_id}: 目录中不存在")
                continue
            try:
                api_key = await self.providers.load_api_key(
                    user_id, definition.provider_id
                )
            except LookupError:
                skipped.append(f"{model_id}: 未配置 API Key")
                continue
            result.append((definition, api_key))
        if tracer is not None:
            async with tracer.step(PREPARE, "model.fallback") as step:
                step.metadata["candidates"] = len(result)
                step.metadata["declared"] = len(candidates)
                if skipped:
                    step.metadata["skipped"] = " | ".join(skipped)
        if not result and candidates:
            # 有声明但一个都不可用：这不是「降级」，是「没有兜底」。必须在
            # 主模型失败之前就留下这句话，否则只能等用户看到 402 才发现。
            logger.warning(
                "model_fallback_unavailable user=%s current=%s skipped=%s",
                user_id,
                current_model_id,
                " | ".join(skipped),
            )
        return result


def _summary_to_session_usage(summary: Summary) -> SessionUsage:
    return SessionUsage(
        total_tokens=summary.total_tokens,
        cached_prompt_tokens=summary.cached_prompt_tokens,
        uncached_prompt_tokens=summary.uncached_prompt_tokens,
        completion_tokens=summary.completion_tokens,
        cache_hit_rate=summary.cache_hit_rate,
        total_runs=summary.total_runs,
        cache_reported_runs=summary.cache_reported_runs,
        estimated_runs=summary.estimated_runs,
    )


def _normalize_request(request: Request) -> Request:
    request.request_id = request.request_id.strip()
    request.user_id = request.user_id.strip()
    request.session_id = request.session_id.strip()
    request.message = request.message.strip()
    request.model_id = request.model_id.strip()
    request.reasoning_option_id = request.reasoning_option_id.strip()
    request.image_ids = [i.strip() for i in request.image_ids]
    request.upload_paths = [p.strip() for p in request.upload_paths]
    return request


def _validate_request(request: Request) -> Error | None:
    if not request.request_id or not request.user_id or not request.session_id:
        return Error("invalid_request", "requestId, userId, and sessionId are required")
    if not request.message and not request.image_ids and not request.upload_paths:
        return Error("invalid_request", "message, imageIds, or uploadPaths is required")
    return None


def _parse_memory_json(raw: str) -> list[str]:
    """解析 LLM 返回的 JSON 数组，带多种兜底策略。"""
    if not raw:
        return []

    raw = raw.strip()

    # 策略 1：直接解析
    try:
        data = json.loads(raw)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return data
    except json.JSONDecodeError:
        pass

    # 策略 2：提取 Markdown 代码块中的 JSON
    code_block_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1))
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return data
        except json.JSONDecodeError:
            pass

    # 策略 3：找第一个 '[' 到最后一个 ']' 之间的内容
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return data
        except json.JSONDecodeError:
            pass

    # 策略 4：用正则提取引号之间的字符串（最宽松的兜底；兼容 ASCII 与中文引号）
    strings = re.findall(r'["“]([^"”]+)["”]', raw)
    if strings:
        # 过滤单字符噪音（如 "a"），但保留 2+ 字符的短事实（如 "偏好A"）
        valid = [s for s in strings if len(s) >= 2]
        return valid[:3]  # 最多取 3 条

    # 全部失败，返回空
    logger.warning("auto_memory: failed to parse JSON from: %s", raw[:200])
    return []
