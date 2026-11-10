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

from openai import APIStatusError, AsyncOpenAI, BadRequestError
from openai.types.completion_usage import CompletionUsage

from ..agentstream.decoder import StreamDecoder
from ..agentstream.types import Event
from ..models.catalog import ModelDefinition
from ..session.store import SessionStore
from ..skills.budget import bind_skill_load_budget, new_skill_load_budget
from ..tools import TOOL_FAILURE, tool_error_result
from ..trace import (
    DEGRADE_SUMMARY,
    LLM_CALL,
    SUMMARY,
    TOOL_CALL,
    VERIFY,
    Tracer,
    new_tracer,
)
from ..usage.types import Tokens
from .configuration import ConfiguredRun
from .progress import ProgressRecorder
from .run_log import RunEventLog
from .run_plan import STATUS_DONE, RunPlanRegistry, render_plan_block
from .tool_result import (
    Materialized,
    count_oversized_tool_results,
    materialize_tool_result,
)

# 工具调用轮次上限：安全网而非调度器（正常任务不该撞到，仅异常循环时兜底）。
# 耗尽时 end_reason=rounds_exhausted 且保持 task_status=in_progress，下次可续跑。
MAX_TOOL_ROUNDS = int(os.getenv("EDITH_MAX_TOOL_ROUNDS", "30"))


def elapsed_ms(started_at: float) -> int:
    """自 started_at（time.monotonic 起点）以来的整数毫秒数。"""
    return round((time.monotonic() - started_at) * 1000)


# 当前 run 的 token 预算（占模型上下文窗口比例）：超过则主动收尾，避免硬撞 context_length_exceeded
TOOL_CONTEXT_BUDGET_RATIO = 0.8
# 历史压缩触发线：估算口径改成 CJK 感知后（见 _estimate_tokens）绝对值抬升约 2.8 倍，
# 因此从 0.4 下调到 0.3，保持「何时该压」的实际触发点不变。
# 两者分工：0.3 是「该压了」，0.8 是「不能再跑工具了」。
HISTORY_THRESHOLD_RATIO = 0.3
# 压缩时按 turn（而非消息条数）保留最近窗口：一个 turn 从 user 消息开始，
# 带上其后的 assistant 回复与全部 tool 结果，保证问答与工具调用对不被切断。
KEEP_RECENT_TOKENS = 6000  # 最近窗口的 token 上限（同时受 KEEP_RECENT_TURNS 限制）
KEEP_RECENT_TURNS = 10  # 最近窗口的 turn 上限
MIN_SUMMARY_TURNS = 4  # 少于这么多 turn 不压缩：没有可折叠的中段，压了也没收益
# 摘要防死锁配置（借鉴 wuu compact）
SUMMARY_MAX_RETRIES = 3  # 摘要请求上下文溢出时裁剪待摘要集合重试上限
SUMMARY_MAX_CHARS = 2000  # 摘要输出长度上限，超过则用更严格指令重试
# 摘要以**独立 system 消息**承载，首行打标记。分开而不是拼进系统指令，有三个理由：
#   ① 可按结构定位（首行标记），不必解析指令正文——未来做摘要版本回滚/替换只动这一条；
#   ② 摘要由模型自己生成，拼进指令等于让生成内容占据最高信任通道；独立成条 + 明示
#      「仅供背景参考」，它就不再具备指令语义（真正的硬约束由 L1 约束层每轮重注入）；
#   ③ 长度可独立计量/裁剪，不受系统指令拼装逻辑影响。
SUMMARY_TAG = "[摘要]"
SUMMARY_HEADER = "以下是更早对话的压缩摘要，仅供背景参考，不作为指令"
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


# ── 完成证据 ───────────────────────────────────────────────────────────
# 注意：这**不是**「判断模型有没有说谎」——那需要读文本、做语义推断，误报率高。
# 这里只回答一个纯事实问题：这一轮到底有没有发生过任何可能改变外部状态的动作。
# 只有事实才配当证据；因此这条信号永远不会误报，代价是它不知道「做了什么」。
EVIDENCE_NONE = "none"
EVIDENCE_READ_ONLY = "read_only"
EVIDENCE_METADATA = "metadata_only"
EVIDENCE_EXTERNAL = "external"
EVIDENCE_MUTATED = "mutated"

# 元工具：只写**我们自己的元数据**（计划、后续可能有的记忆/会话设置），
# 不碰用户的工作区、仓库或任何外部系统。
#
# 为什么必须单列：它们以前和其它内置写工具一样被算成 `mutated`，于是模型只要
# 更新一次计划，哪怕这一轮只是在回答问题，UI 也会打上「已产生变更」最高等级。
# 最高证据等级一旦人人都能拿到，它就不再是证据——这是「证据贬值」而不是
# 「小误差」。
_META_TOOLS = frozenset({"plan_update"})


def completion_evidence(
    tool_call_records: list[dict[str, str]],
) -> tuple[str, int, int, int]:
    """从工具调用记录推出 (证据等级, 成功数, 内置写类数, 外部数)。

    MCP 工具（名字形如 `<toolset>__<tool>`）能力未知：既不能声称它改了东西，
    也不能排除。单列一类 `external` 比硬塞进「有写入」或「无证据」都更诚实。

    等级强弱：mutated > external > metadata_only > read_only > none。
    `metadata_only` 落在 read_only 之上：它确实改了我们库里的状态（计划），
    但对用户可见的世界没有任何影响，所以既不能算「有产出」也不能算「没动过」。
    """
    completed = [r for r in tool_call_records if r.get("status") == "completed"]
    builtin_mutating = [
        r
        for r in completed
        if "__" not in r.get("name", "")
        and not _is_readonly_tool(r.get("name", ""))
        and r.get("name", "") not in _META_TOOLS
    ]
    external = [r for r in completed if "__" in r.get("name", "")]
    metadata = [r for r in completed if r.get("name", "") in _META_TOOLS]

    if builtin_mutating:
        level = EVIDENCE_MUTATED
    elif external:
        level = EVIDENCE_EXTERNAL
    elif metadata:
        level = EVIDENCE_METADATA
    elif completed:
        level = EVIDENCE_READ_ONLY
    else:
        level = EVIDENCE_NONE
    return level, len(completed), len(builtin_mutating), len(external)


async def _record_completion_evidence(
    tracer: Tracer,
    request_id: str,
    tool_call_records: list[dict[str, str]],
    finish_reason: str | None,
    end_reason: str,
    extra: dict | None = None,
) -> str:
    """把「完成声明」与「事实」并排落进 trace，并让无证据的 completed 显式可见。

    为什么不阻断、不改语义：`completed` 的既有含义是「模型这轮没再要工具」，
    大量正常场景（纯问答、只读查询）本来就没有写入。真正的问题是它被 UI 当成
    「任务已完成」展示——这需要证据等级，而不是一个错误码。
    """
    level, ok, mutating, external = completion_evidence(tool_call_records)
    async with tracer.step(
        VERIFY,
        "completion",
        metadata={
            "evidence": level,
            "tools_ok": ok,
            "mutating_ok": mutating,
            "external_ok": external,
            "finish_reason": finish_reason or "",
            "end_reason": end_reason,
            **(extra or {}),
        },
    ):
        pass
    if end_reason == "completed" and level == EVIDENCE_NONE:
        # 这正是「0 工具调用却声称已创建文件」那一类：现在它是一行日志 + 一条
        # trace，可以统计发生率，而不是靠用户偶然发现。
        logger.warning(
            "completion_without_evidence request_id=%s finish_reason=%s "
            "tools_ok=0 —— 模型已收尾，但本轮没有任何工具调用",
            request_id,
            finish_reason or "-",
        )
    if end_reason == "completed" and level == EVIDENCE_METADATA:
        # 只更新了计划：这说明模型在「汇报进度」，但这一步没有任何可核对产出。
        # 不升级成 warning——纯问答里顺手维护计划是正常行为；但要能和
        # 「mutated」分开统计，否则最高证据等级会被计划更新稀释。
        logger.info(
            "completion_metadata_only request_id=%s —— 本轮只有元工具调用（如 plan_update）",
            request_id,
        )
    unverified = int((extra or {}).get("plan_unverified") or 0)
    if unverified:
        # 理论上进不来（校验不过的更新会被整批拒绝）。能走到这里，说明某条路径
        # 把 done 写进了计划却没走 resolve_evidence —— 这是校验被绕过的信号。
        logger.warning(
            "plan_done_without_verification request_id=%s end_reason=%s ids=%s",
            request_id,
            end_reason,
            (extra or {}).get("plan_unverified_ids", ""),
        )
    return level


def _estimate_text_tokens(text: str) -> int:
    """估算一段文本的 token（CJK ≈ 0.7/字，其余 ≈ 1/4 字符）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * 0.7) + (len(text) - cjk) // 4


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """估算消息列表 token（全局唯一实现）。

    除 content 外还要算 tool_calls 的 JSON——工具调用参数（尤其是写文件正文）
    是上下文的大头，只算 content 会严重低估，让预算与阈值双双失效。
    """
    total = 0
    for m in messages:
        text = m.get("content") or ""
        if isinstance(text, str):
            total += _estimate_text_tokens(text)
        tool_calls = m.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False, default=str)) // 4
    return total


def _estimate_tools_tokens(tools: list[dict] | None) -> int:
    """估算工具定义（schema）占用的 token。

    schema 是**每个请求都要发一遍**的固定前缀：20 个内置工具 + MCP 工具加起来
    几千 token 很常见。只按 `context_window × 比例` 算预算会把它整段漏掉——
    那是「必然存在但从不计入」的隐性开销，在工具多的 agent 里足以让
    「看起来没超」的请求其实已经贴到窗口上限。

    schema 是纯 JSON（几乎没有 CJK），按 4 字符/token 估即可。
    """
    if not tools:
        return 0
    return len(json.dumps(tools, ensure_ascii=False, default=str)) // 4


def _split_into_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把消息拆成 turn 列表。

    一个 turn 从 user 消息开始，直到下一个 user 之前的所有消息（assistant、
    tool、工具结果）都属于它。这保证任何切分点都落在 user 边界上：
    - assistant(tool_calls) 与它的 tool 结果不会被切开（不会产出非法消息序列）；
    - 一问一答不会被切开（EDITH 的历史表里工具消息不落库，若按 assistant
      也开新 turn，则「一个 turn」就等于「一条消息」，对齐形同虚设）。
    """
    turns: list[list[dict[str, Any]]] = []
    for msg in messages:
        if msg.get("role") == "user" or not turns:
            turns.append([msg])
        else:
            turns[-1].append(msg)
    return turns


def _recent_msgs(
    messages: list[dict[str, Any]], keep_tokens: int, max_turns: int | None = None
) -> list[dict[str, Any]]:
    """从尾部往前保留最近若干个完整 turn。

    keep_tokens 与 max_turns 两个上限同时生效，取先到的那个；
    无论如何至少保留最后一个 turn，否则本轮对话就没了。
    """
    if not messages:
        return []
    turns = _split_into_turns(messages)
    if not turns:
        return []
    kept: list[list[dict[str, Any]]] = []
    total = 0
    for turn in reversed(turns):
        if max_turns is not None and len(kept) >= max_turns:
            break
        turn_tokens = _estimate_tokens(turn)
        if kept and total + turn_tokens > keep_tokens:
            break
        kept.insert(0, turn)
        total += turn_tokens
    return [m for turn in kept for m in turn]


def _audit_oversized_tool_results(messages: list[dict[str, Any]]) -> int:
    """只读审计：历史里是否还有超预算的工具结果。

    旧实现在这里逐轮把较早的工具结果削短。那是 run 内唯一让前缀缓存失效的
    自我伤害动作（实测：稳态命中 ≈99%，`trimmed=1` 的轮次掉到 43%~61%），
    而且它改写的正是「模型当时看到的事实」。

    现在裁剪前移到结果诞生时（`materialize_tool_result`），历史一旦写入就不可变，
    这里只负责把「绕过定型的路径」暴露出来——有告警就说明有入口漏了，要修那个入口，
    而不是在这里静默削短。
    """
    oversized = count_oversized_tool_results(messages)
    if oversized:
        logger.warning("history_has_oversized_tool_results count=%d", oversized)
    return oversized


def _record_materialize(step: Any, materialized: Materialized) -> None:
    """把定型指标挂到 tool step 上：三个数就能回答「削了多少、怎么削的」。

    没有这三个数，「工具结果太大导致缓存/成本问题」只能靠猜；有了它们，
    可以按工具聚合出「谁在产大结果」，从源头调工具而不是在历史里补刀。
    """
    step.metadata["result_chars"] = materialized.original_chars
    step.metadata["model_chars"] = len(materialized.content)
    step.metadata["materialize"] = materialized.strategy


def _cache_fingerprint(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """请求形态指纹：缓存命中的前提是「从开头起的前缀逐字节一致」。

    run 级用量汇总会把几十轮压成两个数字，既看不出「第几轮开始 miss」，
    也看不出「system 有没有变」。这里只记两类能定责的低成本事实：

    - sys_hash：system 段（指令 + 摘要条）哈希。它一变就说明是**我们自己**
      改写了请求头（摘要更新 / 约束新增 / 记忆写入 / 上传路径变化），
      破坏半径是整个历史，且与模型行为无关——这是最该被证伪的假设。
    - sys_chars：system 段字符数，用来看破坏半径的量级（占 prompt 的比例）。

    历史段不哈希：它每轮本来就追加，哈希必然变化，没有信息量。真正的
    「是否复用」由每轮 cached / prompt 相邻比较得出，不靠猜。
    """
    system_parts: list[str] = []
    for message in messages:
        if message.get("role") == "system":
            system_parts.append(str(message.get("content") or ""))
    joined = "\x00".join(system_parts)
    return {
        "sys_hash": hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12],
        "sys_chars": len(joined),
        "sys_count": len(system_parts),
    }


def render_summary_message(summary: str) -> dict[str, Any]:
    """把摘要渲染成独立 system 消息：首行标记 + 明示「非指令」+ 正文。"""
    return {
        "role": "system",
        "content": f"{SUMMARY_TAG} {SUMMARY_HEADER}\n{summary}",
    }


def is_summary_message(message: dict[str, Any]) -> bool:
    """判断某条消息是否为摘要消息（按首行标记定位，不解析正文）。"""
    return message.get("role") == "system" and (
        message.get("content") or ""
    ).startswith(SUMMARY_TAG)


# 计划块挂在摘要正文之后。之所以塞进同一条 system 而不是再加一条：
# 独立的计划消息会被 `_compact_in_memory` 当作 kept_head 原样保留，压几次就堆几份，
# 互相矛盾的旧计划会和最新状态一起喂给模型。挂在摘要里则永远只有一份。
PLAN_BLOCK_HEADER = "【当前计划】"
_PLAN_BLOCK_SEP = "\n\n" + PLAN_BLOCK_HEADER


def _attach_plan_block(summary: str, plan_block: str) -> str:
    """把计划块接在摘要正文后面（没有计划就原样返回）。"""
    if not plan_block:
        return summary
    body = plan_block
    if body.startswith(PLAN_BLOCK_HEADER):
        body = body[len(PLAN_BLOCK_HEADER) :].lstrip("\n")
    return f"{summary}{_PLAN_BLOCK_SEP}{body}"


def _strip_plan_block(text: str) -> str:
    """丢掉摘要里的计划块：再次压缩时它是「上一次的快照」，不该被当成摘要内容
    带进新的摘要请求（否则模型会把旧计划复述成对话历史）。"""
    index = text.find(_PLAN_BLOCK_SEP)
    if index < 0:
        return text
    return text[:index]


def _current_plan_block(plans: RunPlanRegistry | None, user_id: str, session_id: str) -> str:
    """当前计划的渲染文本（没有活跃计划时为空串）。"""
    if plans is None:
        return ""
    current = plans.current(user_id, session_id)
    if current is None or not current.items:
        return ""
    return render_plan_block(
        current.items,
        header=PLAN_BLOCK_HEADER,
        note="（压缩时刻的计划快照；最新进度以最近一次 plan_update 的结果为准）",
    )


def collapse_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把多条 system 消息合并成一条（保持原相对顺序），其余消息原样。

    这是给「不接受多条 system 消息」的 OpenAI 兼容端点准备的降级形态：
    合并后信息不丢，只是摘要不再独立成条（标记保留在正文里）。
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system_parts.append(message.get("content") or "")
        else:
            rest.append(message)
    if len(system_parts) <= 1:
        return list(messages)
    merged = {"role": "system", "content": "\n\n".join(p for p in system_parts if p)}
    return [merged, *rest]


def _has_multiple_system(messages: list[dict[str, Any]]) -> bool:
    return sum(1 for m in messages if m.get("role") == "system") > 1


def build_messages(
    system_instruction: str,
    history: list[dict[str, Any]],
    recent_history: list[dict[str, Any]],
    summary: str,
    user_message: str,
) -> list[dict[str, Any]]:
    """组装 OpenAI 消息列表：系统指令 + 摘要（可选）+ 历史 + 当前用户消息。

    摘要单独成条（见 SUMMARY_TAG 注释），且固定紧随系统指令之后：
    指令永远是 messages[0]，当前用户消息永远是 messages[-1]。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_instruction}]
    if summary:
        messages.append(render_summary_message(summary))
        messages.extend(recent_history)
    else:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages


def _extract_reasoning(delta: Any) -> str:
    """从 chunk 的 delta 中提取推理增量，兼容不同供应商的字段位置。"""
    # 1. 优先尝试标准的 reasoning_content
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        return reasoning

    # 2. 尝试从 model_extra 中获取 reasoning_content
    model_extra = getattr(delta, "model_extra", None) or {}
    reasoning = model_extra.get("reasoning_content") or model_extra.get("reasoning")
    if reasoning:
        return reasoning

    # 3. 某些本地部署兼容层可能直接把 reasoning 挂在 delta 顶层
    reasoning = getattr(delta, "reasoning", None)
    if reasoning:
        return reasoning

    return ""


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
            cached_prompt_tokens=(
                self._cached if self._cache_known and self._parts else None
            ),
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


SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。把历史对话压缩成结构化摘要，"
    "保留关键事实、用户目标、未完成事项、精确的文件名/函数名/报错信息。"
    "严禁编造内容，只压缩已有信息；不要重复任何密码、密钥或令牌。"
)

# 结构化 7 段。自由文本摘要会随代际退化成「一段模糊要点」，
# 固定分节让「已完成 / 进行中 / 下一步」这类状态位每代都能被显式更新。
INITIAL_SUMMARY_PROMPT = """请把以下对话压缩成结构化摘要：

<对话>
{history}
</对话>

严格使用如下结构，每节保持简洁：

## 目标
[用户想达成什么？可多条]

## 关键事实
- [用户提到的约束、偏好、重要数据]

## 已完成
- [x] ...

## 进行中
- [ ] ...

## 未完成 / 待办
- [ ] ...

## 下一步
1. ...

## 关键上下文
- [继续对话所需的重要信息]

总长度不超过 500 字。"""

UPDATE_SUMMARY_PROMPT = """下面是【上一份摘要】和【新对话内容】。
请把新内容合并进旧摘要，输出一份更新后的结构化摘要。

规则：
- 保留旧摘要里所有仍然有效的信息
- 把新内容里已完成的事项，从"进行中"移到"已完成"
- 更新"下一步"和"未完成"
- 保留精确的文件名、函数名、报错信息
- 不要重复任何密码、密钥或令牌

<上一份摘要>
{previous}
</上一份摘要>

<新对话内容>
{new_history}
</新对话内容>

按同样的结构（目标 / 关键事实 / 已完成 / 进行中 / 未完成 / 下一步 / 关键上下文）输出。
总长度不超过 500 字。"""

SUMMARY_STRICT_SUFFIX = (
    "\n\n（上一次输出过长，请更精简：只保留最关键的事实与未完成事项，"
    "总长度不超过 300 字。）"
)


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
    # 2. 估算 token 并判断是否触发摘要（口径 = 全局唯一的 _estimate_tokens，单位是 turn）
    turns = _split_into_turns(history)
    estimated_tokens = _estimate_tokens(history)
    threshold = int(configured.definition.context_window * HISTORY_THRESHOLD_RATIO)
    # 最近窗口上限：不能大于触发线的 1/3，否则「保留窗口」本身就占满阈值，
    # 压完立刻又超标（小窗口模型上尤其明显）。KEEP_RECENT_TOKENS 只是上限。
    keep_tokens = min(KEEP_RECENT_TOKENS, max(1, threshold // 3))

    new_summary = old_summary  # 默认保持旧摘要不变
    new_summarized_until_id = summarized_until_id  # 默认保持旧值
    # 摘要失败时的兜底窗口（非 None 表示本轮降级为硬截断，且不写库）
    degraded_recent: list[dict[str, Any]] | None = None
    # 摘要调用的用量（含因溢出/超长而重试的每一次，它们都是真实花费）
    summary_usage = _UsageAccumulator()

    # 摘要阶段整体打点：未触发时也能看到「检查本身花了多久」
    async with tracer.step(
        SUMMARY,
        "compress",
        metadata={
            "history": len(history),
            "turns": len(turns),
            "estimated_tokens": estimated_tokens,
            "threshold": threshold,
            "keep_tokens": keep_tokens,
        },
    ) as summary_step:
        # 3. 按 turn 对齐切分：最近窗口保持完整 turn，其余（中段）进摘要
        if estimated_tokens > threshold and len(turns) >= MIN_SUMMARY_TURNS:
            recent = _recent_msgs(history, keep_tokens, max_turns=KEEP_RECENT_TURNS)
            to_summarize = history[: len(history) - len(recent)]
        else:
            recent, to_summarize = [], []
        if to_summarize:
            # 4. 触发摘要：把「旧摘要 + 待折叠中段」压缩成新摘要
            summary_step.metadata["triggered"] = True
            summary_step.metadata["to_summarize"] = len(to_summarize)
            summary_step.metadata["recent"] = len(recent)
            logger.info(
                "summary_triggered user=%s session=%s estimated_tokens=%d threshold=%d "
                "turns=%d to_summarize=%d recent=%d",
                user_id,
                session_id,
                estimated_tokens,
                threshold,
                len(turns),
                len(to_summarize),
                len(recent),
            )

            try:
                # 5. 调用模型生成摘要（注意：必须 await！）
                #    防死锁（借鉴 wuu compact）：
                #    a) 摘要请求本身上下文溢出 → 裁掉待摘要集合较新的一半后重试
                #       （最多 SUMMARY_MAX_RETRIES 次），防「压缩→溢出→再压缩→再溢出」死锁；
                #    b) 摘要输出超长 → 用更严格压缩指令重试 1 次，防摘要反噬上下文；
                #    c) 其它失败 → 降级为「旧摘要 + 最近窗口」硬截断，不写库（见 except）。
                summary_source = to_summarize
                strict = False
                new_summary = old_summary
                for attempt in range(SUMMARY_MAX_RETRIES):
                    history_text = "\n".join(
                        [
                            f"{m.get('role', 'unknown')}: {m.get('content', '')}"
                            for m in summary_source
                        ]
                    )
                    # 旧摘要必须参与新摘要的生成——否则每代整体覆盖上一代，
                    # 早期信息（接口命名、技术选型、用户约束）随代际硬丢失。
                    if old_summary:
                        user_prompt = UPDATE_SUMMARY_PROMPT.format(
                            previous=old_summary, new_history=history_text
                        )
                    else:
                        user_prompt = INITIAL_SUMMARY_PROMPT.format(
                            history=history_text
                        )
                    if strict:
                        user_prompt += SUMMARY_STRICT_SUFFIX
                    prompt_messages = [
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ]
                    try:
                        response = await client.chat.completions.create(
                            model=configured.definition.api_model,
                            messages=prompt_messages,
                            max_tokens=500,  # 限制摘要长度，防止过长
                        )
                        if response.usage is not None:
                            summary_usage.add(tokens_from_usage(response.usage))
                        content = response.choices[0].message.content or ""
                        # 空摘要等于「什么也没压」：此时若推进游标，被折叠的消息就没了。
                        # 当作失败走硬截断，不写库。
                        if not content.strip():
                            raise RuntimeError("summary returned empty content")
                        new_summary = content
                    except Exception as exc:
                        if _is_context_overflow(exc) and len(summary_source) > 1:
                            # 上下文溢出：砍掉较新的一半，只摘要更靠前的连续前缀。
                            # 不能反过来砍最旧的：summarized_until_id 只能标记「连续前缀」，
                            # 砍掉最旧的却把 until_id 推到最后，会让被砍掉的那段永久消失
                            # （既不进摘要也不留在历史里）。保留前缀则未摘要的部分仍留在
                            # 历史中，下一轮还能继续折叠，不静默丢信息。
                            summary_step.metadata["overflow_retries"] = attempt + 1
                            logger.warning(
                                "summary_overflow user=%s session=%s attempt=%d error=%s",
                                user_id,
                                session_id,
                                attempt + 1,
                                str(exc),
                            )
                            summary_source = summary_source[
                                : max(1, len(summary_source) // 2)
                            ]
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
                # 摘要失败：旧摘要不变、不写库，但本轮上下文降级为「旧摘要 + 最近窗口」。
                # 只保留原摘要而放全部历史回去，等于把「压缩失败」变成「压缩没发生」，
                # 下一次请求还是会撞同一个窗口——硬截断才有界。
                degraded_recent = recent
                summary_step.metadata["degraded"] = "hard_trim"
                logger.warning(
                    "summary_fallback user=%s session=%s recent=%d error=%s",
                    user_id,
                    session_id,
                    len(recent),
                    str(e),
                )
                # 降级显式可见（供 trace / 跨 run 聚合）
                tracer.degrade(
                    DEGRADE_SUMMARY, "model_summary", "hard_trim", str(e)[:200]
                )
                # 不抛出异常，继续用旧摘要

        summary_step.metadata["summary_len"] = len(new_summary)

    summary_tokens = summary_usage.tokens()
    if summary_tokens.total_tokens:
        summary_step.metadata["tokens"] = summary_tokens.total_tokens

    # ===== 获取近期消息（摘要之后） =====
    if degraded_recent is not None:
        # 降级路径：用本轮算好的 turn 对齐窗口，不读库（库里没有新的 until_id）
        recent_history = degraded_recent
    else:
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


# run 内压缩的来源上限：摘要请求本身也不能溢出。超出部分从**最早**开始丢，
# 因为它们是最可能已经被旧摘要覆盖、对后续任务价值最低的部分。
INLINE_COMPACT_SOURCE_CHARS = 40000
# 单次 run 内允许的显式压缩次数。每次压缩都会让上下文缩到「最近窗口 + 摘要」，
# 允许 3 次意味着上下文至少被完整翻新过三次——再撞预算说明任务体量已经超出
# 单次 run 的合理范围，收尾让用户确认比继续烧钱更合适。
MAX_INLINE_COMPACTS = 3


def _tool_pairs_intact(messages: list[dict[str, Any]]) -> bool:
    """每条 tool 结果都必须能找到它对应的 assistant tool_call。

    压缩边界理论上只落在 user 消息前（`_split_into_turns` 保证 turn 从 user 开始，
    而 tool 结果永远在同一个 turn 内），所以配对天然不会被拆开。但那是「碰巧成立」
    而不是「被验证过」：一旦将来有人改切分规则（比如允许按 token 数在 turn 内部
    硬切），产出的就是非法消息序列——孤立的 tool 结果，模型会报错或胡乱解读，
    而且这种损坏在压缩当时完全静默。

    所以在压缩结果返回前自检一次。不合法就不压（宁可本轮收尾，也不发坏请求）。
    """
    declared: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                declared.add(str(call["id"]))
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id is None or str(call_id) not in declared:
                return False
    return True


async def _compact_in_memory(
    client: AsyncOpenAI,
    definition: ModelDefinition,
    messages: list[dict[str, Any]],
    budget_tokens: int,
    tracer: Tracer,
    request_id: str,
    plan_block: str = "",
) -> tuple[list[dict[str, Any]], Tokens | None, bool]:
    """run 内的显式压缩：把中段 turn 折叠成一条摘要 system 消息。

    与启动时 `summary_compress` 的分工：
      - 那个压的是**已落库的历史**（跨 run 复用），触发线 0.3；
      - 这个压的是**本 run 里 messages 的增长**（工具结果不断追加），触发线 0.8。

    两者都是「整段替换」而不是「单条改写」：替换会让前缀缓存失效一次，
    但代价明确、次数有限、可观测；而逐轮削短旧工具结果是**每轮都失效**，
    且悄悄改掉了模型当时看到的事实。宁要一次显式的重排，不要持续的隐性漂移。

    plan_block：当前计划的渲染文本。计划是**状态**不是**历史**——一旦被折进摘要，
    模型就再也看不到自己排到第几步了（会重新规划、重复已完成的工作）。所以压掉
    多少对话都不能压掉这份状态：它跟着摘要一起重建，且**只有一份**（否则多次压缩
    会堆出一叠互相矛盾的计划）。
    """
    # 1. 拆出「system 前缀」与「对话段」：指令必须留在 messages[0]，
    #    摘要在它之后，两者都不参与折叠（它们本身就是压缩产物）。
    head: list[dict[str, Any]] = []
    body_start = 0
    while body_start < len(messages) and messages[body_start].get("role") == "system":
        head.append(messages[body_start])
        body_start += 1
    body = messages[body_start:]
    if not head or not body:
        return messages, None, False

    old_summary = ""
    kept_head: list[dict[str, Any]] = []
    for message in head:
        if is_summary_message(message):
            old_summary = _strip_plan_block((message.get("content") or "").split("\n", 1)[-1])
        else:
            kept_head.append(message)

    # 2. 最近窗口按 turn 对齐保留，其余进摘要（与启动时压缩同一套切分规则）
    keep_tokens = min(KEEP_RECENT_TOKENS, max(1, budget_tokens // 4))
    recent = _recent_msgs(body, keep_tokens, max_turns=KEEP_RECENT_TURNS)
    to_fold = body[: len(body) - len(recent)] if recent else body
    if not to_fold:
        return messages, None, False

    # 3. 来源按字符预算截取（保留较新的中段），避免摘要请求自身溢出
    source: list[dict[str, Any]] = []
    used = 0
    for message in reversed(to_fold):
        text = str(message.get("content") or "")
        if used + len(text) > INLINE_COMPACT_SOURCE_CHARS and source:
            break
        source.insert(0, message)
        used += len(text)
    dropped = len(to_fold) - len(source)

    history_text = "\n".join(
        f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in source
    )
    if dropped:
        history_text = (
            f"（更早的 {dropped} 条消息因体积过大未纳入本次摘要，其要点已尽量包含在"
            f"既有摘要中）\n{history_text}"
        )
    if old_summary:
        user_prompt = UPDATE_SUMMARY_PROMPT.format(
            previous=old_summary, new_history=history_text
        )
    else:
        user_prompt = INITIAL_SUMMARY_PROMPT.format(history=history_text)

    usage_acc = _UsageAccumulator()
    async with tracer.step(
        SUMMARY,
        "inline_compact",
        metadata={
            "to_fold": len(to_fold),
            "folded": len(source),
            "dropped": dropped,
            "recent": len(recent),
            "msgs_before": len(messages),
        },
    ) as step:
        try:
            response = await client.chat.completions.create(
                model=definition.api_model,
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001 - 压不动就交给调用方收尾
            step.metadata["failed"] = str(exc)[:200]
            logger.warning(
                "inline_compact_failed request_id=%s error=%s",
                request_id,
                str(exc)[:200],
            )
            return messages, None, False
        if response.usage is not None:
            usage_acc.add(tokens_from_usage(response.usage))
        summary = (response.choices[0].message.content or "").strip()
        if not summary:
            step.metadata["failed"] = "empty_summary"
            return messages, None, False

    compacted = [
        *kept_head,
        render_summary_message(_attach_plan_block(summary, plan_block)),
        *recent,
    ]
    # 自检：压缩结果不能产出孤立的 tool 结果（见 `_tool_pairs_intact`）
    if not _tool_pairs_intact(compacted):
        step.metadata["rejected"] = "orphan_tool_result"
        logger.warning(
            "inline_compact_rejected request_id=%s reason=orphan_tool_result",
            request_id,
        )
        return messages, None, False
    step.metadata["msgs_after"] = len(compacted)
    step.metadata["summary_len"] = len(summary)
    logger.info(
        "inline_compact request_id=%s before=%d after=%d folded=%d recent=%d",
        request_id,
        len(messages),
        len(compacted),
        len(source),
        len(recent),
    )
    tokens = usage_acc.tokens()
    return compacted, (tokens if tokens.total_tokens else None), True


async def chat(
    configured: ConfiguredRun,
    store: SessionStore,
    events: RunEventLog,
    assistant_id: str,
    tools: list[dict] | None = None,
    tool_executor=None,  # tool_executor->main.py:ToolRegistry:tool.py
    mcp_tools=None,
    fallback_models: list[tuple[ModelDefinition, str]] | None = None,
    started_at: float | None = None,
    tracer: Tracer | None = None,
    plans: RunPlanRegistry | None = None,
    progress: ProgressRecorder | None = None,
) -> tuple[Tokens, str, int, str, int, str]:
    """执行一次流式对话（含工具调用循环）。

    返回 (累计用量, 实际使用的模型 ID, 工具调用次数, 终止原因, 本轮耗时 ms, 完成证据等级)。

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
    # L2 预算（单 run 的 Skill 正文额度）在这里绑定：ContextVar 的作用域是本 task，
    # `asyncio.create_task` 会复制上下文，因此两个并发 run 各记各的账；run 结束随
    # task 消失，不需要 TTL 或清理钩子。**每次 run 都新建**，所以覆盖写入即幂等。
    skill_budget = new_skill_load_budget()
    bind_skill_load_budget(skill_budget)
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
    # 工具 schema 是每个请求都带的固定前缀（几十个工具可达数 K token），
    # 必须从 prompt 预算里扣掉：它不体现在 messages 里，却真实占用窗口。
    tools_tokens = _estimate_tools_tokens(all_tools)

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

    def emit(event: Event) -> None:
        """唯一出口：先送事件日志（可重放），再喂进度落库（刷新后可见）。

        两条消费者共用一个入口而不是各自订阅，是因为「发出去的事实」必须只有
        一份来源——否则一处忘记通知，就会出现「实时有、刷新后没有」的不对称。
        """
        events.publish(event)
        if progress is not None:
            progress.observe(event)

    # 活跃计划：把本 run 的工具调用记录（同一 list 对象，边跑边追加）与事件通道
    # 交给 plan_update 工具，让它能核对「标 done 的那一项到底有没有对应调用」。
    # 关闭由 service 负责（它要读取最终快照后再关）。
    if plans is not None:
        plans.open(
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            facts=tool_call_records,
            emit=emit,
        )

    def _plan_meta() -> dict:
        """计划进度并入完成证据：done 与 verified 的差就是「声称完成却没有证据」。

        正常情况下两者相等（校验不过的更新会被拒绝），所以一旦不等，说明有更新
        绕过了校验路径——这正是需要被看见的异常，而不是可以忽略的噪声。
        因此这里除了计数，还要把未核实的项 id 一并交出：只有 id 能让人回到具体
        是哪一步，只给一个数字等于「知道有问题但查不到」。
        """
        if plans is None:
            return {}
        current = plans.current(user_id, session_id)
        if current is None or not current.items:
            return {}
        unverified = current.unverified_done()
        meta = {
            "plan_total": len(current.items),
            "plan_done": sum(1 for item in current.items if item.status == STATUS_DONE),
            "plan_verified": sum(1 for item in current.items if item.verified),
            "plan_unverified": len(unverified),
        }
        if unverified:
            meta["plan_unverified_ids"] = ",".join(unverified)
        return meta

    def _run_meta() -> dict:
        """本轮收尾要并进 trace 的全部横切事实。

        Skill 加载账目放这里而不是单独一个 step：它在**跑完之后**才有值，
        单独 step 只能在循环结束时补一条，而补的那条如果中途异常就永久缺失；
        挂在 completion 这条必然执行的步骤上，才能保证"要么有数，要么没跑完"。
        """
        return {**_plan_meta(), **skill_budget.snapshot()}

    # 收尾时保留"最后一轮"的文本/推理（循环前初始化，避免预算首轮 break 时未定义）
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # 实际使用的模型（首轮沿回退链确定；先给初值，避免异常路径未绑定）
    active_definition: ModelDefinition = configured.definition
    used_model_id = configured.definition.id
    inline_compacts = 0

    for _round in range(MAX_TOOL_ROUNDS):
        # C1 历史不可变审计：裁剪已在结果诞生时完成，这里只读、不改写历史。
        oversized = _audit_oversized_tool_results(messages)
        # C2 token 预算：接近模型上下文窗口上限时，先做一次**显式压缩**，
        # 压完仍超才收尾。历史增长的正确处理方式是「整段替换成摘要」，
        # 不是「逐轮把旧消息削短」——后者每轮都破坏前缀缓存，还篡改了
        # 模型当时看到的事实。
        #
        # 口径：窗口 × 0.8 是 prompt 的可用额度（剩下 20% 留给输出与估误差），
        # 再扣掉工具 schema。0.8 是**触发策略**而非安全保证——估算本身有误差，
        # 所以它是「该动手了」的信号，不是「一定不会溢出」的承诺。
        budget = max(
            1024,
            int(configured.definition.context_window * TOOL_CONTEXT_BUDGET_RATIO)
            - tools_tokens,
        )
        if _estimate_tokens(messages) > budget:
            if inline_compacts < MAX_INLINE_COMPACTS:
                # 计划是状态不是历史：压掉多少对话都不能压掉它，否则模型会忘记
                # 自己排到第几步（重新规划 / 重复已完成的工作）。
                messages, compact_tokens, compacted = await _compact_in_memory(
                    client,
                    active_definition,
                    messages,
                    budget,
                    tracer,
                    request_id,
                    plan_block=_current_plan_block(plans, user_id, session_id),
                )
                if compacted:
                    inline_compacts += 1
                    usage_acc.add(compact_tokens)
            else:
                compacted = False
            if not compacted or _estimate_tokens(messages) > budget:
                end_reason = "context_budget_exhausted"
                logger.warning(
                    "context budget exhausted request_id=%s budget=%d compacts=%d",
                    request_id,
                    budget,
                    inline_compacts,
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
            # 请求形态与治理动作：诊断缓存 miss 只需回答「哪一段变了」，
            # 不留下这两个字段，后续任何优化都无法被证明有效。
            # oversized 代替原来的 trimmed：历史已不可变，正常恒为 0；
            # 非 0 就是在说「有入口绕过了定型」，是必须修代码的信号，不是治理手段。
            llm_step.metadata["oversized"] = oversized
            # 预算口径也要落盘：否则「为什么这轮收尾了」只能靠反推估算公式
            llm_step.metadata["budget"] = budget
            llm_step.metadata["tools_tokens"] = tools_tokens
            llm_step.metadata.update(_cache_fingerprint(messages))
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
            finish_reason: str | None = None
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
                # finish_reason 全项目此前从未读取——length 截断是「假完成」的
                # 最硬诱因（输出被 max_tokens 硬砍，不是模型主动收尾）。
                # getattr 兼容不携带该字段的供应商流/测试桩。
                fr = getattr(chunk.choices[0], "finish_reason", None)
                if fr:
                    finish_reason = fr
                # 获取推理内容
                reasoning = _extract_reasoning(delta)
                if reasoning:
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - call_started) * 1000
                    reasoning_parts.append(reasoning)
                    # 非阻塞入队
                    emit(
                        decoder.content_delta("reasoning", "reasoning.delta", reasoning)
                    )
                if delta.content:
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - call_started) * 1000
                    if first_content_ms is None:
                        first_content_ms = (time.monotonic() - call_started) * 1000
                    content_parts.append(delta.content)
                    emit(
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
            if finish_reason is not None:
                # 采集进 trace：截断率（finish_reason=length 占比）是健康指标
                llm_step.metadata["finish_reason"] = finish_reason
            if usage is not None:
                llm_step.metadata["tokens"] = usage.total_tokens
                # 逐轮记账（唯一能算出「跨 run 前缀复用率」的地方）。
                # cached 保留原始形态、不归一化：None = 供应商没上报该字段，
                # 0 = 上报了且真的没命中。混成一个数就等于把统计假象
                # 当成结论——这正是缓存问题此前一直无法定案的原因。
                cached_details = getattr(usage, "prompt_tokens_details", None)
                round_cached = (
                    getattr(cached_details, "cached_tokens", None)
                    if cached_details is not None
                    else None
                )
                llm_step.metadata["prompt"] = usage.prompt_tokens
                llm_step.metadata["cached"] = round_cached
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
                "ttft_ms=%s content_chars=%d reasoning_chars=%d tool_calls=%d tokens=%s "
                "prompt=%s cached=%s oversized=%d sys=%s finish_reason=%s",
                request_id,
                _round + 1,
                round((time.monotonic() - call_started) * 1000),
                llm_step.metadata.get("create_ms"),
                llm_step.metadata.get("ttft_ms", "-"),
                len(content),
                len(reasoning),
                len(tool_calls),
                usage.total_tokens if usage is not None else "-",
                llm_step.metadata.get("prompt", "-"),
                llm_step.metadata.get("cached", "-"),
                oversized,
                llm_step.metadata.get("sys_hash", "-"),
                finish_reason or "-",
            )

        # 收尾判定：本轮无工具调用，或输出被 length 硬截断（截断时流里的工具
        # 片段同样不可信——参数流被砍断，执行残缺调用比不执行更危险）。
        # end_reason 反映「最后一轮」的 finish_reason（每轮局部变量，不跨轮）：
        #   stop / 未上报 → 维持 completed（模型主动收尾，原行为）
        #   length        → output_truncated（外力打断非模型决定，不判完成；
        #                   service 侧因此不清 checkpoint，下次消息可续跑）
        #   content_filter → content_filtered（同属外力打断，不判完成）
        if not tool_calls or finish_reason == "length":
            if finish_reason == "length":
                end_reason = "output_truncated"
                logger.warning(
                    "llm output truncated request_id=%s round=%d tool_calls_seen=%d "
                    "content_chars=%d —— 非模型主动收尾，保持 in_progress 供续跑",
                    request_id,
                    _round + 1,
                    len(tool_calls),
                    len(content),
                )
            elif finish_reason == "content_filter":
                end_reason = "content_filtered"
                logger.warning(
                    "llm content filtered request_id=%s round=%d content_chars=%d",
                    request_id,
                    _round + 1,
                    len(content),
                )
            duration = elapsed_ms(started_at)
            # 完成证据：先算再落库，保证「终态事件」与「刷新后历史」同源同值
            # （和 duration_ms 一样的约束：UI 实时值与历史值必须来自同一次计算）
            evidence = await _record_completion_evidence(
                tracer,
                request_id,
                tool_call_records,
                finish_reason,
                end_reason,
                extra=_run_meta(),
            )
            if content or reasoning or tool_call_records:
                # 先停进度写，再写终态：否则一条迟到的进度写会用「没有结论」的
                # 版本覆盖刚算好的耗时/证据/终止原因。
                if progress is not None:
                    await progress.stop()
                await store.add_assistant_message(
                    user_id,
                    session_id,
                    content,
                    reasoning,
                    workspace_id,
                    tool_call_records,
                    duration,
                    evidence,
                    end_reason,
                    request_id,
                )
            # 收尾（end_reason 仅诊断，不参与恢复判定；恢复只看 task_status + checkpoint）
            await tracer.flush()
            return (
                usage_acc.tokens(),
                used_model_id,
                tool_calls_count,
                end_reason,
                duration,
                evidence,
            )

        # 有工具调用：发 tool.started，执行，发 tool.finished，回传结果继续循环
        ordered = [tool_calls[i] for i in sorted(tool_calls)]
        for tc in ordered:
            emit(
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

        async def _run_one(tc: dict) -> tuple[str, Materialized, float]:
            """执行单个工具，返回 (原始结果, 定型结果, 耗时 ms)。

            定型在这里做而不是在后处理循环里：它是「结果诞生」的同一步，
            也只有在 tool step 作用域内才能把定型指标挂到对应调用上。
            原始结果与定型结果是两个消费者：前端/审计拿原始，模型历史拿定型。
            """
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
                            "tool_cache_hit request_id=%s tool_name=%s",
                            request_id,
                            name,
                        )
                        materialized = materialize_tool_result(name, cached)
                        _record_materialize(tool_step, materialized)
                        return cached, materialized, 0.0
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
                # trace 保留原始结果（审计/排障要看真东西），模型历史用定型版本
                tool_step.output = result
                materialized = materialize_tool_result(
                    name, result, is_error=_parse_result_retryable(result) is False
                )
                _record_materialize(tool_step, materialized)
                return result, materialized, (time.monotonic() - t0) * 1000

        # 只读工具可并发；含写工具则整批串行（一票否决，对齐 Pi 调度策略）
        if len(ordered) > 1 and all(_is_readonly_tool(tc["name"]) for tc in ordered):
            executed = await asyncio.gather(*[_run_one(tc) for tc in ordered])
        else:
            executed = [await _run_one(tc) for tc in ordered]

        # 后处理严格按调用顺序（保证 checkpoint / 熔断 / 事件 / 消息顺序确定）
        for tc, (result, materialized, duration_ms) in zip(ordered, executed):
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
            emit(decoder.tool_finished(tc["id"], status, result))
            # 进入模型历史的是**定型版本**：这是「历史不可变」的起点。
            # 从这里往后，无论跑多少轮、要不要压缩，这条消息的内容都不再变化，
            # 前缀缓存才可能被复用。前端/审计仍拿上面的原始 result。
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": materialized.content,
                }
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
    evidence = await _record_completion_evidence(
        tracer,
        request_id,
        tool_call_records,
        finish_reason,
        end_reason,
        extra=_run_meta(),
    )
    if progress is not None:
        await progress.stop()
    await store.add_assistant_message(
        user_id,
        session_id,
        "".join(content_parts),
        "".join(reasoning_parts),
        workspace_id,
        tool_call_records,
        duration,
        evidence,
        end_reason,
        request_id,
    )
    return (
        usage_acc.tokens(),
        used_model_id,
        tool_calls_count,
        end_reason,
        duration,
        evidence,
    )


async def _create_stream(
    client: AsyncOpenAI,
    definition: ModelDefinition,
    messages: list[dict[str, Any]],
    all_tools: list[dict],
) -> Any:
    """发起一次流式补全请求（首轮形状的唯一来源，便于「合并 system」后原样重试）。"""
    return await client.chat.completions.create(
        model=definition.api_model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        extra_body=definition.extra_fields or None,
        tools=all_tools or None,
    )


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

    消息形态兼容：摘要单独成 system 消息，但部分 OpenAI 兼容端点只接受一条
    system。请求形状被拒（4xx）且当前确实有多条 system 时，就地合并成一条重试一次——
    合并写回 messages 本身，后续工具轮次沿用，不需要逐轮降级。
    """
    candidates: list[tuple[ModelDefinition, str]] = [
        (configured.definition, configured.api_key)
    ]
    if fallback_models:
        candidates.extend(fallback_models)
    last_error: Exception | None = None
    total = len(candidates)
    for index, (definition, api_key) in enumerate(candidates, start=1):
        attempt_started = time.monotonic()
        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=definition.base_url,
                max_retries=2,
            )

            try:
                stream = await _create_stream(client, definition, messages, all_tools)
            except APIStatusError as exc:
                if exc.status_code not in (400, 422) or not _has_multiple_system(
                    messages
                ):
                    raise
                messages[:] = collapse_system_messages(messages)
                if attempts is not None:
                    attempts.append(
                        {
                            "model": definition.id,
                            "form": "collapsed_system",
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                            "duration_ms": round(
                                (time.monotonic() - attempt_started) * 1000
                            ),
                        }
                    )
                logger.warning(
                    "multi system messages rejected model=%s status=%s, retry collapsed",
                    definition.id,
                    exc.status_code,
                )
                attempt_started = time.monotonic()
                stream = await _create_stream(client, definition, messages, all_tools)
            return client, definition, stream
        except Exception as exc:  # noqa: BLE001 - 尝试下一个候选模型
            last_error = exc
            if attempts is not None:
                attempts.append(
                    {
                        "model": definition.id,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "duration_ms": round(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                    }
                )
            # 日志必须说清「还有没有下一个」：无条件打印 trying next，会让
            # 「全链失败」和「切到备用模型」在日志里长得一模一样（402 事故
            # 就是这么被误读成「一直在重试」的）。
            logger.warning(
                "model call failed model=%s attempt=%d/%d error=%s %s",
                definition.id,
                index,
                total,
                exc,
                "→ 尝试下一个候选" if index < total else "→ 候选链已耗尽",
            )
    assert last_error is not None
    # 一行总结：候选链到底试了哪些模型。没有这句，排查只能靠把上面 N 条
    # warning 拼起来看。
    logger.error(
        "model_chain_exhausted candidates=%s last_error=%s",
        [definition.id for definition, _ in candidates],
        last_error,
    )
    raise last_error
