"""链路追踪（trace）模块的数据结构与常量。

与 `internal/volume/trace.py` 无关系——后者是 E2B HTTP 调试开关，本包是
「一次 run 的分步耗时/异常记录」。

设计依据：`docs/trace-eval-design.md` §3。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

# ── step 类型（对齐 docs/trace-eval-design.md §2.3 的枚举） ──────────────
AGENTRUN = "agentrun"
PREPARE = "prepare"
MCP = "mcp"
RESUME = "resume"
SUMMARY = "summary"
LLM_CALL = "llm_call"
TOOL_CALL = "tool_call"
MEMORY = "memory"

# ── trace 状态 ────────────────────────────────────────────────────────
RUNNING = "running"
OK = "ok"
CANCELED = "canceled"
ERROR = "error"

STATUS_LABELS = {
    RUNNING: "进行中",
    OK: "成功",
    CANCELED: "已取消",
    ERROR: "失败",
}

# 降级类型（EDITH 有三条降级链，必须显式可见）
DEGRADE_MODEL = "model_fallback"
DEGRADE_SUMMARY = "summary_fallback"
DEGRADE_SANDBOX = "sandbox_fallback"

# 文本摘要上限；EDITH_TRACE_FULL=1 时存原文截断（排错临时开，见 README 隐私约定）
_DIGEST_LIMIT = 200
_FULL_LIMIT = 4000


def full_text_enabled() -> bool:
    """是否记录原文（默认关：多租户下 trace 不应默认落用户内容原文）。"""
    return os.getenv("EDITH_TRACE_FULL", "").strip().lower() in ("1", "true", "yes")


def as_text(value: Any) -> str:
    """把任意值转成文本（dict/list 走 JSON），供摘要与长度统计使用。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def summarize(value: Any) -> tuple[str, int]:
    """把任意值压缩为 (摘要文本, 原始长度)。

    默认只存 digest（截断 + sha256 前缀 + 长度）；`EDITH_TRACE_FULL=1` 时
    直接存原文截断，便于本地排错。
    """
    text = as_text(value)
    if not text:
        return "", 0
    if full_text_enabled():
        return text[:_FULL_LIMIT], len(text)
    if len(text) <= _DIGEST_LIMIT:
        return text, len(text)
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{text[:_DIGEST_LIMIT]}…(sha256:{h}, len={len(text)})", len(text)


@dataclass
class TraceStep:
    """一次 run 里的一个步骤。字段与 `trace_steps` 表一一对应。"""

    type: str
    name: str
    seq: int = 0
    parent_seq: int | None = None  # None = 顶层
    depth: int = 0
    started_at: str = ""
    duration_ms: float = 0.0
    status: str = OK  # ok / error
    error_type: str | None = None
    error_message: str | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    input_len: int | None = None
    output_len: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 业务侧在 with 块里写 `s.output = ...`；退出时被压成摘要，不进 to_row 的原文列
    output: Any = None

    def to_row(self, request_id: str) -> tuple:
        """转成 `trace_steps` 的插入参数元组（顺序与 INSERT 列一致）。"""
        return (
            request_id,
            self.seq,
            self.parent_seq,
            self.depth,
            self.type,
            self.name,
            self.started_at,
            self.duration_ms,
            self.status,
            self.error_type,
            self.error_message,
            self.input_digest,
            self.output_digest,
            self.input_len,
            self.output_len,
            json.dumps(self.metadata, ensure_ascii=False, default=str)
            if self.metadata
            else None,
        )


@dataclass
class Trace:
    """一次用户输入（= 一次 run）的完整记录。"""

    request_id: str
    user_id: str
    session_id: str
    workspace_id: str | None = None
    user_input: str = ""
    model_id: str | None = None
    started_at: str = ""
    finished_at: str | None = None  # None = 未收尾（进行中 / 崩溃）
    status: str = RUNNING
    end_reason: str | None = None
    duration_ms: int | None = None  # 墙钟
    self_time_ms: int | None = None  # 顶层 step 之和
    total_tokens: int | None = None
    tools_called: int | None = None
    degraded: bool = False
    degradations: list[dict[str, str]] = field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None
    steps: list[TraceStep] = field(default_factory=list)

    def unattributed_ms(self) -> int | None:
        """墙钟 - 自耗时 = 尚未打点的时间（驱动下一步埋点）。"""
        if self.duration_ms is None or self.self_time_ms is None:
            return None
        return max(self.duration_ms - self.self_time_ms, 0)

    def recompute_self_time(self) -> int:
        """自耗时 = 顶层 step 耗时之和（嵌套子步不重复计入）。"""
        self.self_time_ms = round(
            sum(s.duration_ms for s in self.steps if s.parent_seq is None)
        )
        return self.self_time_ms
