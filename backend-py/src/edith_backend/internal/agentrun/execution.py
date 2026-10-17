"""执行层：用 OpenAI SDK 手写流式循环与工具调用循环，产出渠道中性事件。

对应 Go 版 internal/agentrun/execution.go + stream.go 的框架执行部分。
方案 B 不依赖 trpc-agent-go，直接用 AsyncOpenAI 流式调用，并自行实现工具调用循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any

from openai import AsyncOpenAI, BadRequestError
from openai.types.completion_usage import CompletionUsage

from ..agentstream.decoder import StreamDecoder
from ..agentstream.types import Event
from ..models.catalog import ModelDefinition
from ..session.store import SessionStore
from ..tools import TOOL_FAILURE, tool_error_result
from ..trace import (
    DEGRADE_SUMMARY,
    LLM_CALL,
    SUMMARY,
    TOOL_CALL,
    Tracer,
    new_tracer,
)
from ..usage.types import Tokens
from .configuration import ConfiguredRun

# 工具调用轮次上限：安全网而非调度器（正常任务不该撞到，仅异常循环时兜底）。
# 耗尽时 end_reason=rounds_exhausted 且保持 task_status=in_progress，下次可续跑。
MAX_TOOL_ROUNDS = int(os.getenv("EDITH_MAX_TOOL_ROUNDS", "30"))


def elapsed_ms(started_at: float) -> int:
    """自 started_at（time.monotonic 起点）以来的整数毫秒数。"""
    return round((time.monotonic() - started_at) * 1000)
# 当前 run 的 token 预算（占模型上下文窗口比例）：超过则主动收尾，避免硬撞 context_length_exceeded
TOOL_CONTEXT_BUDGET_RATIO = 0.8
HISTORY_THRESHOLD_RATIO = 0.4  # 对齐 Go 版 sessionSummaryContextRatio
# 摘要防死锁配置（借鉴 wuu compact）
SUMMARY_MAX_RETRIES = 3  # 摘要请求上下文溢出时裁剪最旧消息重试上限
SUMMARY_MAX_CHARS = 2000  # 摘要输出长度上限，超过则用更严格指令重试
TOOL_FAILURE_CIRCUIT_BREAKER = 3  # 同工具连续 N 次系统故障（tool_failure）触发熔断
logger = logging.getLogger(__name__)


class ToolCircuitBreakerError(Exception):
    """同一工具连续 tool_failure 达到阈值，编排层熔断，终止 run 并明确报错。"""

    def __init__(self, tool_name: str, failures: int, last_error: str) -> None:
        self.tool_name = tool_name
        self.failures = failures
        self.last_error = last_error
        super().__init__(
            f"工具 {tool_name} 连续 {failures} 次系统故障，已熔断停止。"
            f"最后一次错误：{last_error}"
        )


def _parse_result_retryable(result: str) -> bool | None:
    """从工具结果解析 retryable：False=系统故障（参与熔断），True=业务错误（可自救），None=无该字段（视为成功）。"""
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and "retryable" in data:
        return bool(data["retryable"])
    return None


def _digest(value: str, limit: int = 200) -> str:
    """生成简短进度摘要（不存全文）：短则原样，长则截断 + sha256 前缀 + 长度。"""
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{text[:limit]}…(sha256:{h}, len={len(text)})"


def _read_progress(tool_name: str, result: str) -> str:
    """从 read_file 结果提取结构化进度（供「已读摘要」）；其余工具返回空串。"""
    if tool_name != "sandbox_read_file":
        return ""
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict) or "totalLines" not in data:
        return ""
    return json.dumps(
        {
            "path": data.get("path", ""),
            "startLine": data.get("startLine"),
            "endLine": data.get("endLine"),
            "totalLines": data.get("totalLines"),
        },
        ensure_ascii=False,
    )


# 只读工具：一轮中若全部是只读工具，可并发执行；含任一写工具则整批串行（对齐 Pi 的调度策略）。
_READ_ONLY_TOOLS = frozenset({"sandbox_read_file", "sandbox_list_files"})


def _is_readonly_tool(name: str) -> bool:
    """判断工具是否只读（MCP 工具未知，保守视为非只读）。"""
    return name in _READ_ONLY_TOOLS


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估算消息列表 token：CJK ≈ 0.7/字，其余 ≈ 1/4 字符。"""
    total = 0
    for m in messages:
        text = m.get("content") or ""
        if not isinstance(text, str):
            continue
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        total += int(cjk * 0.7) + (len(text) - cjk) // 4
    return total


def _trim_tool_results(
    messages: list[dict[str, Any]], keep_recent: int = 6, limit: int = 1500
) -> None:
    """把较早轮次的工具结果截断（保留最近 keep_recent 条全文），抑制上下文膨胀。"""
    indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    to_trim = indices if keep_recent <= 0 else indices[:-keep_recent]
    for i in to_trim:
        content = messages[i].get("content") or ""
        if len(content) > limit:
            messages[i]["content"] = (
                content[:limit] + f"\n…（较早的工具结果已截断，原 {len(content)} 字符）"
            )


def build_messages(
    system_instruction: str,
    history: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
    summary: str,
    user_message: str,
) -> list[dict[str, Any]]:
    """组装 OpenAI 消息列表：系统指令 + 历史 + 当前用户消息。"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_instruction}]
    if summary:
        messages[0]["content"] += "\n\n【之前对话摘要】\n" + summary
        messages.extend(recent_history)
    else:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages


def _extract_reasoning(delta: Any) -> str:
    """从 chunk 的 delta 中提取推理增量，兼容不同供应商的字段位置。"""
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        return reasoning
    model_extra = getattr(delta, "model_extra", None) or {}
    return model_extra.get("reasoning_content", "") or ""


def tokens_from_usage(usage: CompletionUsage | None) -> Tokens:
    """把 OpenAI 用量转为 usage.Tokens。"""
    if usage is None:
        return Tokens()
    cached = None
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
    return Tokens(
        prompt_tokens=usage.prompt_tokens,
        cached_prompt_tokens=cached,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


class _UsageAccumulator:
    """跨轮次累加用量，并保留「这些数字有多可信」的信息。

    两条不变量：
    - reported：任一轮是本地估算，整轮就标成估算（估算值不能冒充真实值）。
    - cached_prompt_tokens：任一轮没上报缓存，整轮缓存即未知（不把 None 当 0 求和，
      否则会低估命中率；也不假装已知）。
    """

    def __init__(self) -> None:
        self._prompt = 0
        self._completion = 0
        self._total = 0
        self._cached = 0
        self._cache_known = True
        self._reported = True
        self._parts = 0

    def add(self, part: Tokens | None) -> None:
        if part is None:
            return
        self._prompt += part.prompt_tokens
        self._completion += part.completion_tokens
        self._total += part.total_tokens
        self._reported = self._reported and part.reported
        if part.cached_prompt_tokens is None:
            self._cache_known = False
        else:
            self._cached += part.cached_prompt_tokens
        self._parts += 1

    def tokens(self) -> Tokens:
        return Tokens(
            prompt_tokens=self._prompt,
            cached_prompt_tokens=self._cached if self._cache_known and self._parts else None,
            completion_tokens=self._completion,
            total_tokens=self._total,
            reported=self._reported,
        )


def _is_context_overflow(exc: Exception) -> bool:
    """判断是否上下文溢出错误（摘要防死锁触发条件）。

    兼容 OpenAI SDK 的 BadRequestError（code=context_length_exceeded），
    以及其他供应商/网关返回的文本形式错误。
    """
    if isinstance(exc, BadRequestError):
        if getattr(exc, "code", None) == "context_length_exceeded":
            return True
        body = getattr(exc, "body", None)
        message = str(body) if body is not None else str(exc)
        return "maximum context length" in message.lower()
    message = str(exc).lower()
    return "context_length_exceeded" in message or "maximum context length" in message


async def summary_compress(
    configured: ConfiguredRun,
    store: SessionStore,
    user_id: str,
    session_id: str,
    client: AsyncOpenAI,
    tracer: Tracer,
) -> tuple[list[dict[str, Any]], Tokens | None]:
    """必要时压缩历史，返回 (组装好的 messages, 摘要调用自身的用量)。

    摘要本身是一次真实的模型调用（把整段历史塞进 prompt），此前完全不计量，
    导致「成本优化机制」的开销在用量里不可见。第二个返回值为 None 表示本次
    没有触发摘要调用。
    """

    # 1. 读取当前摘要状态
    summary_data = await store.get_summary(user_id, session_id)
    if summary_data is not None:
        summarized_until_id = summary_data["summarized_until_id"]
        old_summary = summary_data["summary"]
    else:
        summarized_until_id = 0
        old_summary = ""

    history = await store.get_messages(
        user_id, session_id, after_id=summarized_until_id
    )
    # 2. 粗估 token，判断是否触发摘要
    estimated_tokens = sum(len(m.get("content", "")) for m in history) // 4
    threshold = int(configured.definition.context_window * HISTORY_THRESHOLD_RATIO)

    new_summary = old_summary  # 默认保持旧摘要不变
    new_summarized_until_id = summarized_until_id  # 默认保持旧值
    # 摘要调用的用量（含因溢出/超长而重试的每一次，它们都是真实花费）
    summary_usage = _UsageAccumulator()

    # 摘要阶段整体打点：未触发时也能看到「检查本身花了多久」
    async with tracer.step(
        SUMMARY,
        "compress",
        metadata={
            "history": len(history),
            "estimated_tokens": estimated_tokens,
            "threshold": threshold,
        },
    ) as summary_step:
        if estimated_tokens > threshold and len(history) > 10:
            # 3. 触发摘要：把旧摘要 + 全部历史压缩成新摘要
            summary_step.metadata["triggered"] = True
            logger.info(
                "summary_triggered user=%s session=%s estimated_tokens=%d threshold=%d",
                user_id,
                session_id,
                estimated_tokens,
                threshold,
            )

            try:
                # 4. 调用模型生成摘要（注意：必须 await！）
                #    防死锁（借鉴 wuu compact）：
                #    a) 摘要请求本身上下文溢出 → 裁剪最旧一半消息重试（最多 SUMMARY_MAX_RETRIES 次），
                #       防「压缩→溢出→再压缩→再溢出」死锁；
                #    b) 摘要输出超长 → 用更严格压缩指令重试 1 次，防摘要反噬上下文。
                summary_source = history
                strict = False
                new_summary = old_summary
                for attempt in range(SUMMARY_MAX_RETRIES):
                    history_text = "\n".join(
                        [
                            f"{m.get('role', 'unknown')}: {m.get('content', '')}"
                            for m in summary_source
                        ]
                    )
                    prompt_messages = [
                        {
                            "role": "system",
                            "content": (
                                "你是对话摘要助手，请把用户与助手的对话压缩成简洁的要点摘要，"
                                "保留关键事实、用户目标与未完成事项。注意不要重复任何密码或密钥。"
                                + (
                                    "请尽量精简，只保留最关键的事实与未完成事项，总长度不超过 500 字。"
                                    if strict
                                    else ""
                                )
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"请摘要以下对话：\n\n{history_text}",
                        },
                    ]
                    try:
                        response = await client.chat.completions.create(
                            model=configured.definition.api_model,
                            messages=prompt_messages,
                            max_tokens=500,  # 限制摘要长度，防止过长
                        )
                        if response.usage is not None:
                            summary_usage.add(tokens_from_usage(response.usage))
                        new_summary = response.choices[0].message.content or old_summary
                    except Exception as exc:
                        if _is_context_overflow(exc) and len(summary_source) > 1:
                            # 上下文溢出：裁剪最旧一半消息，重试
                            summary_step.metadata["overflow_retries"] = attempt + 1
                            logger.warning(
                                "summary_overflow user=%s session=%s attempt=%d error=%s",
                                user_id,
                                session_id,
                                attempt + 1,
                                str(exc),
                            )
                            summary_source = summary_source[len(summary_source) // 2 :]
                            continue
                        raise
                    # 生成成功：输出超长则用更严格指令重试一次
                    if len(new_summary) > SUMMARY_MAX_CHARS and not strict:
                        logger.warning(
                            "summary_too_long user=%s session=%s len=%d retry_strict",
                            user_id,
                            session_id,
                            len(new_summary),
                        )
                        strict = True
                        continue
                    break
                else:
                    raise RuntimeError("summary retries exhausted")

                # 5. 更新 summarized_until_id：只标记实际被折叠的（可能被裁剪过的）消息
                if summary_source:
                    new_summarized_until_id = summary_source[-1].get(
                        "id", summarized_until_id
                    )
                else:
                    new_summarized_until_id = summarized_until_id

                # 6. 保存到数据库
                await store.save_summary(
                    user_id, session_id, new_summarized_until_id, new_summary
                )
                logger.info(
                    "summary_saved user=%s session=%s summarized_until_id=%d summary_len=%d",
                    user_id,
                    session_id,
                    new_summarized_until_id,
                    len(new_summary),
                )
            except Exception as e:  # noqa: BLE001 - 摘要失败降级，不阻塞主流程
                # 摘要失败：降级，保持旧摘要不变，但打 warning
                logger.warning(
                    "summary_fallback user=%s session=%s error=%s",
                    user_id,
                    session_id,
                    str(e),
                )
                # 降级显式可见（供 trace / 跨 run 聚合）
                tracer.degrade(
                    DEGRADE_SUMMARY, "model_summary", "keep_old_summary", str(e)[:200]
                )
                # 不抛出异常，继续用旧摘要

        summary_step.metadata["summary_len"] = len(new_summary)

    summary_tokens = summary_usage.tokens()
    if summary_tokens.total_tokens:
        summary_step.metadata["tokens"] = summary_tokens.total_tokens

    # ===== 获取近期消息（摘要之后） =====
    recent_history = await store.get_messages(
        user_id, session_id, after_id=new_summarized_until_id
    )

    # ===== 组装消息 =====
    messages = build_messages(
        configured.system_instruction,
        history,
        recent_history,
        new_summary,  # 使用最新的摘要（可能是新生成的，也可能是旧的）
        configured.message,
    )

    return messages, (summary_tokens if summary_tokens.total_tokens else None)


async def chat(
    configured: ConfiguredRun,
    store: SessionStore,
    queue: asyncio.Queue[Event | None],
    assistant_id: str,
    tools: list[dict] | None = None,
    tool_executor=None,  # tool_executor->main.py:ToolRegistry:tool.py
    mcp_tools=None,
    fallback_models: list[tuple[ModelDefinition, str]] | None = None,
    started_at: float | None = None,
    tracer: Tracer | None = None,
) -> tuple[Tokens, str, int, str, int]:
    """执行一次流式对话（含工具调用循环），返回 (累计用量, 实际使用的模型 ID, 工具调用次数, 终止原因, 本轮耗时 ms)。

    第一轮模型调用沿「当前模型 → 回退候选」尝试：openai SDK 的 max_retries=2
    已对网络/5xx/超时等可重试错误做指数退避重试，重试后仍失败才回退到下一个
    候选模型；后续工具轮次复用确定的模型。同时把 assistant 回复持久化到会话存储。

    started_at：本轮任务起点（time.monotonic）。由调用方传入以覆盖 MCP 连接等准备阶段；
    缺省则以本函数入口为起点。耗时随 assistant 消息一并落库，刷新历史仍可见。
    tracer：链路追踪打点器；缺省为空转（不影响任何行为）。
    """
    if started_at is None:
        started_at = time.monotonic()
    user_id = configured.request.user_id
    session_id = configured.request.session_id
    workspace_id = configured.request.workspace_id or None
    request_id = getattr(configured.request, "request_id", "unknown")
    workspace_id_arg = configured.request.workspace_id or ""
    end_reason = "completed"  # 终止原因（仅诊断；恢复依据是 task_status，与它解耦）
    if tracer is None:
        tracer = new_tracer(
            None,
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
        )
    client = AsyncOpenAI(
        api_key=configured.api_key,
        base_url=configured.definition.base_url,
        max_retries=2,
    )

    # 摘要压缩（摘要调用自身的用量也归属本次 run）
    messages, summary_tokens = await summary_compress(
        configured, store, user_id, session_id, client, tracer
    )
    # 有图片时把当前用户消息转为多模态 content
    if configured.image_urls:
        messages[-1]["content"] = [
            {"type": "text", "text": configured.message},
            *[
                {"type": "image_url", "image_url": {"url": url}}
                for url in configured.image_urls
            ],
        ]

    # 合并静态工具与本次 Run 打开的 MCP 工具
    all_tools: list[dict] = list(tools) if tools else []
    if mcp_tools is not None:
        all_tools.extend(mcp_tools.tool_definitions())

    decoder = StreamDecoder(assistant_id)
    usage_acc = _UsageAccumulator()
    usage_acc.add(summary_tokens)
    tool_calls_count = 0  # 本次运行累计工具调用次数（跨轮次累加）
    consecutive_failures: dict[str, int] = (
        {}
    )  # 各工具连续 tool_failure 次数（跨轮累计，成功/command_error 清零）
    # run 级只读结果缓存：同一 run 内重复读同一文件时复用，省去重复的沙箱往返（写操作后清空）
    read_cache: dict[str, str] = {}
    # 本次新增：累积本轮工具调用记录，收尾时随 assistant 消息持久化（供刷新后历史展示）
    tool_call_records: list[dict[str, str]] = []

    # 收尾时保留"最后一轮"的文本/推理（循环前初始化，避免预算首轮 break 时未定义）
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # 实际使用的模型（首轮沿回退链确定；先给初值，避免异常路径未绑定）
    active_definition: ModelDefinition = configured.definition
    used_model_id = configured.definition.id

    for _round in range(MAX_TOOL_ROUNDS):
        # C1 上下文治理：截断较早轮次的工具结果（保留最近若干条全文）
        _trim_tool_results(messages)
        # C2 token 预算：接近模型上下文窗口上限时主动收尾，避免硬撞 context_length_exceeded
        budget = int(configured.definition.context_window * TOOL_CONTEXT_BUDGET_RATIO)
        if _estimate_tokens(messages) > budget:
            end_reason = "context_budget_exhausted"
            logger.warning(
                "context budget exhausted request_id=%s budget=%d", request_id, budget
            )
            break
        content_parts = []
        reasoning_parts = []
        tool_calls: dict[int, dict[str, str]] = {}
        usage: CompletionUsage | None = None

        # 一次模型调用 = 一个 llm_call 步：覆盖 create + 全量流式消费。
        #   create_ms        到响应头（首轮含回退链的失败尝试）
        #   ttft_ms          到第一个增量（推理或正文）——「LLM 一直在思考」看这个
        #   first_content_ms 到第一段正文（纯推理很长时它远大于 ttft_ms）
        attempts: list[dict] = []
        async with tracer.step(
            LLM_CALL,
            f"create#{_round + 1}",
            input={"messages": len(messages)},
            metadata={"round": _round + 1, "msgs": len(messages)},
        ) as llm_step:
            # 同一列表对象：_first_model_call 里的失败尝试会就地追加
            llm_step.metadata["attempts"] = attempts
            call_started = time.monotonic()
            if _round == 0:
                client, active_definition, stream = await _first_model_call(
                    configured, fallback_models, messages, all_tools, attempts
                )
                used_model_id = active_definition.id
            else:
                stream = await client.chat.completions.create(
                    model=active_definition.api_model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=active_definition.extra_fields or None,
                    tools=all_tools or None,
                )
            llm_step.metadata["model"] = active_definition.api_model
            llm_step.metadata["create_ms"] = round(
                (time.monotonic() - call_started) * 1000
            )
            logger.info(
                "llm_call_start request_id=%s round=%d model=%s msgs=%d attempts=%d",
                request_id,
                _round + 1,
                active_definition.api_model,
                len(messages),
                len(attempts),
            )

            ttft_ms: float | None = None
            first_content_ms: float | None = None
            # 流式处理
            async for chunk in stream:
                # usage 只出现在流末尾那个「choices 为空」的收尾块里
                # （stream_options={"include_usage": True} 的协议约定）。
                # 必须在跳过空 choices 之前捕获，否则真实用量永远拿不到，
                # 只能退化成本地估算（估算不含缓存字段 → 命中率恒为未知）。
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 获取推理内容
                reasoning = _extract_reasoning(delta)
                if reasoning:
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - call_started) * 1000
                    reasoning_parts.append(reasoning)
                    # 非阻塞入队
                    queue.put_nowait(
                        decoder.content_delta("reasoning", "reasoning.delta", reasoning)
                    )
                if delta.content:
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - call_started) * 1000
                    if first_content_ms is None:
                        first_content_ms = (time.monotonic() - call_started) * 1000
                    content_parts.append(delta.content)
                    queue.put_nowait(
                        decoder.content_delta("text", "message.delta", delta.content)
                    )
                    # 兼容openai
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        slot = tool_calls.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            if ttft_ms is not None:
                llm_step.metadata["ttft_ms"] = round(ttft_ms)
            if first_content_ms is not None:
                llm_step.metadata["first_content_ms"] = round(first_content_ms)
            llm_step.metadata["content_chars"] = len(content)
            llm_step.metadata["reasoning_chars"] = len(reasoning)
            llm_step.metadata["tool_calls"] = len(tool_calls)
            if usage is not None:
                llm_step.metadata["tokens"] = usage.total_tokens
                usage_acc.add(tokens_from_usage(usage))
            else:
                # 供应商流里没有 usage 块：本地估算兜底，并显式标记为估算，
                # 避免估算值在下游被当成真实上报（估算不含缓存字段）。
                prompt_est = _estimate_tokens(messages)
                completion_est = _estimate_tokens([{"content": content + reasoning}])
                llm_step.metadata["tokens_estimated"] = True
                usage_acc.add(
                    Tokens(
                        prompt_tokens=prompt_est,
                        cached_prompt_tokens=None,
                        completion_tokens=completion_est,
                        total_tokens=prompt_est + completion_est,
                        reported=False,
                    )
                )
            logger.info(
                "llm_call_end request_id=%s round=%d duration_ms=%d create_ms=%s "
                "ttft_ms=%s content_chars=%d reasoning_chars=%d tool_calls=%d tokens=%s",
                request_id,
                _round + 1,
                round((time.monotonic() - call_started) * 1000),
                llm_step.metadata.get("create_ms"),
                llm_step.metadata.get("ttft_ms", "-"),
                len(content),
                len(reasoning),
                len(tool_calls),
                usage.total_tokens if usage is not None else "-",
            )

        if not tool_calls:
            duration = elapsed_ms(started_at)
            if content or reasoning or tool_call_records:
                await store.add_assistant_message(
                    user_id,
                    session_id,
                    content,
                    reasoning,
                    workspace_id,
                    tool_call_records,
                    duration,
                )
            # 模型主动收尾（end_reason 仅诊断，不参与恢复判定）
            await tracer.flush()
            return usage_acc.tokens(), used_model_id, tool_calls_count, end_reason, duration

        # 有工具调用：发 tool.started，执行，发 tool.finished，回传结果继续循环
        ordered = [tool_calls[i] for i in sorted(tool_calls)]
        for tc in ordered:
            queue.put_nowait(
                decoder.tool_started(tc["id"], tc["name"], tc["arguments"])
            )

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in ordered
        ]
        messages.append(assistant_msg)

        async def _run_one(tc: dict) -> tuple[str, float]:
            """执行单个工具，返回 (结果 JSON, 耗时 ms)。仅执行，后处理在下方统一做。"""
            t0 = time.monotonic()
            name = tc["name"]
            is_mcp = mcp_tools is not None and mcp_tools.has(name)
            source = "mcp" if is_mcp else "builtin"
            async with tracer.step(
                TOOL_CALL,
                name,
                input=tc.get("arguments", ""),
                metadata={"source": source},
            ) as tool_step:
                # run 级去重：只读工具命中相同调用时复用缓存结果，跳过实际执行
                cache_key = ""
                if _is_readonly_tool(name):
                    cache_key = name + "\x00" + (tc["arguments"] or "")
                    cached = read_cache.get(cache_key)
                    if cached is not None:
                        tool_step.metadata["cached"] = True
                        logger.info(
                            "tool_cache_hit request_id=%s tool_name=%s", request_id, name
                        )
                        return cached, 0.0
                logger.info(
                    "tool_call_start request_id=%s tool_name=%s source=%s",
                    request_id,
                    name,
                    source,
                )
                try:
                    arguments = (
                        json.loads(tc["arguments"]) if tc["arguments"].strip() else {}
                    )
                except json.JSONDecodeError:
                    arguments = {}
                try:
                    if is_mcp:
                        result = await mcp_tools.execute(name, arguments)
                    elif tool_executor is not None:
                        # 透传当前会话 workspace_id，使 Sandbox 工具层能自动补全工作区路径
                        result = await tool_executor.execute(
                            name, arguments, user_id, session_id, workspace_id_arg
                        )
                    else:
                        result = json.dumps(
                            {
                                "error": "no tool executor",
                                "kind": TOOL_FAILURE,
                                "retryable": False,
                            },
                            ensure_ascii=False,
                        )
                except Exception as exc:  # 兜底：MCP 等漏网异常
                    # 修复可观测性缺口：此前异常被吞成 result 字符串、不留日志
                    logger.exception(
                        "tool_call_error request_id=%s tool_name=%s", request_id, name
                    )
                    result = tool_error_result(name, exc)
                if cache_key:
                    read_cache[cache_key] = result
                tool_step.output = result
                return result, (time.monotonic() - t0) * 1000

        # 只读工具可并发；含写工具则整批串行（一票否决，对齐 Pi 调度策略）
        if len(ordered) > 1 and all(_is_readonly_tool(tc["name"]) for tc in ordered):
            executed = await asyncio.gather(*[_run_one(tc) for tc in ordered])
        else:
            executed = [await _run_one(tc) for tc in ordered]

        # 后处理严格按调用顺序（保证 checkpoint / 熔断 / 事件 / 消息顺序确定）
        for tc, (result, duration_ms) in zip(ordered, executed):
            tool_name = tc["name"]
            is_mcp = mcp_tools is not None and mcp_tools.has(tool_name)
            source = "mcp" if is_mcp else "builtin"
            tool_calls_count += 1
            # 写操作可能改变文件内容：清空只读缓存，避免后续返回过期结果
            if not _is_readonly_tool(tool_name):
                read_cache.clear()
            # WAL：本次工具调用完成后立即写进度日志（先记录，再做熔断判断，
            # 确保即使随后熔断/中断，本次进度也已落盘；独立于 run 终止）
            try:
                await store.append_checkpoint(
                    user_id,
                    session_id,
                    tool_calls_count,
                    tool_name,
                    _digest(tc.get("arguments", "")),
                    _digest(result),
                    _read_progress(tool_name, result),
                )
            except Exception as exc:  # noqa: BLE001 - 进度写入失败不阻塞主流程
                logger.warning("append checkpoint failed: %s", exc)
            # 熔断统计：同一工具连续系统故障（retryable=False）达到阈值即终止；成功 / 业务错误 / 无字段都重置
            retryable = _parse_result_retryable(result)
            if retryable is False:
                consecutive_failures[tool_name] = (
                    consecutive_failures.get(tool_name, 0) + 1
                )
                if consecutive_failures[tool_name] >= TOOL_FAILURE_CIRCUIT_BREAKER:
                    raise ToolCircuitBreakerError(
                        tool_name, consecutive_failures[tool_name], result
                    )
            else:
                consecutive_failures[tool_name] = 0
            is_error = retryable is False

            logger.info(
                "tool_call_end request_id=%s tool_name=%s source=%s duration_ms=%.2f result_len=%d is_error=%s",
                request_id,
                tool_name,
                source,
                duration_ms,
                len(result),
                is_error,
            )
            status = "failed" if retryable is False else "completed"
            queue.put_nowait(decoder.tool_finished(tc["id"], status, result))
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": result}
            )
            # 记录工具调用（供刷新后历史展示）；result 截断，避免 DB 膨胀
            tool_call_records.append(
                {
                    "id": tc["id"],
                    "name": tool_name,
                    "arguments": tc.get("arguments", ""),
                    "status": status,
                    "result": result[:2000],
                }
            )

        # 每轮结束落盘一次（一次 commit）：即使进程随后被杀，已完成轮次的步骤也已可查
        await tracer.flush()

    # 循环结束：预算耗尽已在循环内设 end_reason；否则视为轮次耗尽
    # （end_reason 仅诊断/UI，不参与恢复判定——是否恢复只看 task_status + checkpoint）
    if end_reason == "completed":
        end_reason = "rounds_exhausted"
    # 保留最后一轮的推理与文本：此前 reasoning 硬编码为空，导致刷新后历史里丢失全部推理
    duration = elapsed_ms(started_at)
    await store.add_assistant_message(
        user_id,
        session_id,
        "".join(content_parts),
        "".join(reasoning_parts),
        workspace_id,
        tool_call_records,
        duration,
    )
    return usage_acc.tokens(), used_model_id, tool_calls_count, end_reason, duration


async def _first_model_call(
    configured: ConfiguredRun,
    fallback_models: list[tuple[ModelDefinition, str]] | None,
    messages: list[dict[str, Any]],
    all_tools: list[dict],
    attempts: list[dict] | None = None,
) -> tuple[AsyncOpenAI, ModelDefinition, Any]:
    """第一轮模型调用：沿「当前模型 → 回退候选」尝试，返回 (client, definition, stream)。

    openai SDK 的 max_retries=2 已对网络/5xx/超时等可重试错误做指数退避重试；
    候选之间是模型级回退，失败原因只打 warning 不中断，全部失败才抛出最后一个错误。

    attempts：传入列表时，每次失败尝试的 (模型, 错误, 耗时) 会就地追加，
    使「模型全链失败」在链路追踪里可逐条定位（此前只有一行 warning）。
    """
    candidates: list[tuple[ModelDefinition, str]] = [
        (configured.definition, configured.api_key)
    ]
    if fallback_models:
        candidates.extend(fallback_models)
    last_error: Exception | None = None
    for definition, api_key in candidates:
        attempt_started = time.monotonic()
        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=definition.base_url,
                max_retries=2,
            )
            stream = await client.chat.completions.create(
                model=definition.api_model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=definition.extra_fields or None,
                tools=all_tools or None,
            )
            return client, definition, stream
        except Exception as exc:  # noqa: BLE001 - 尝试下一个候选模型
            last_error = exc
            if attempts is not None:
                attempts.append(
                    {
                        "model": definition.id,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "duration_ms": round((time.monotonic() - attempt_started) * 1000),
                    }
                )
            logger.warning(
                "model call failed model=%s error=%s, trying next",
                definition.id,
                exc,
            )
    assert last_error is not None
    raise last_error
