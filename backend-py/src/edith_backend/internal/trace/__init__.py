"""链路追踪模块：一次 run 的分步耗时 / 异常 / 降级记录。

对外接口：
- `new_tracer(...)` / `Tracer.step(...)`：打点（业务侧唯一需要碰的东西）
- `create_schema(db)` / `Store(db)`：落盘（由 `main.py` 组合根装配）
- `viewer`：CLI 查看器（`python -m edith_backend.internal.trace.viewer`）
"""

from .schema import create_schema
from .store import Store
from .tracer import Tracer, format_traceback, new_tracer, now_iso
from .types import (
    AGENTRUN,
    CANCELED,
    DEGRADE_MODEL,
    DEGRADE_SANDBOX,
    DEGRADE_SUMMARY,
    ERROR,
    LLM_CALL,
    MCP,
    MEMORY,
    OK,
    PREPARE,
    RESUME,
    RUNNING,
    STATUS_LABELS,
    SUMMARY,
    TOOL_CALL,
    Trace,
    TraceStep,
)

__all__ = [
    "AGENTRUN",
    "CANCELED",
    "DEGRADE_MODEL",
    "DEGRADE_SANDBOX",
    "DEGRADE_SUMMARY",
    "ERROR",
    "LLM_CALL",
    "MCP",
    "MEMORY",
    "OK",
    "PREPARE",
    "RESUME",
    "RUNNING",
    "STATUS_LABELS",
    "SUMMARY",
    "TOOL_CALL",
    "Store",
    "Trace",
    "TraceStep",
    "Tracer",
    "create_schema",
    "format_traceback",
    "new_tracer",
    "now_iso",
]
