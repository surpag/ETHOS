"""渠道中性事件模型。

对应 Go 版 internal/agentstream/types.go。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class APIError(BaseModel):
    """描述一次任务启动或执行错误。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    message: str


class SessionUsage(BaseModel):
    """一次会话累计用量的渠道中性格式。

    缓存类字段可能缺失（模型/平台未上报）：调用方必须按「没有就是没有」处理，
    不能当 0 参与算术。total_runs / cache_reported_runs / estimated_runs 说明
    这些数字覆盖了多少次运行，供 UI 标注可信范围。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    total_tokens: int
    cached_prompt_tokens: int | None = None
    uncached_prompt_tokens: int | None = None
    completion_tokens: int
    cache_hit_rate: float | None = None
    total_runs: int = 0
    cache_reported_runs: int = 0
    estimated_runs: int = 0


class Event(BaseModel):
    """AgentRun 向所有渠道输出的唯一事件格式。

    字段与 Go 版 agentstream.Event 完全一致（camelCase 序列化）。
    usage 字段显式序列化为 sessionUsage，对齐 Go 的 json:"sessionUsage,omitempty"，
    前端 RunCompletedEvent/RunCanceledEvent 按 sessionUsage 读取；
    构造时仍可用 usage= 或 sessionUsage= 传入。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    session_id: str | None = None
    request_id: str | None = None

    assistant_id: str | None = None
    block_id: str | None = None
    block_type: str | None = None
    delta: str | None = None

    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: str | None = None
    tool_status: str | None = None
    tool_result: str | None = None

    usage: SessionUsage | None = Field(default=None, alias="sessionUsage")
    error: APIError | None = None
    fallback: str | None = None
    # 终止原因（诊断/UI 用，与恢复依据 task_status 解耦）：
    # completed=模型主动收尾；rounds_exhausted=工具轮次耗尽未完成
    end_reason: str | None = None
    # 本轮耗时（毫秒）：run.started → 终态事件。仅终态事件携带，供 UI 展示"完成时长"
    duration_ms: int | None = None
