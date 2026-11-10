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


class PlanItemSnapshot(BaseModel):
    """一项计划的状态快照。verified 只由服务端判定，模型无法自己写 True。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    status: str
    # True = 该 done 项的证据已通过服务端核对；None = 未标完成
    verified: bool | None = None
    evidence: dict | None = None


class PlanSnapshot(BaseModel):
    """一次 run 的计划快照（整份、带 revision，前端据此幂等覆盖）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    request_id: str
    revision: int
    updated_at: str = ""
    items: list[PlanItemSnapshot] = Field(default_factory=list)


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
    # 事件在该 run 内的单调序号（1 起）。由 RunEventLog 统一赋值，不靠调用方。
    # 它的唯一用途是断流重连：客户端带上「已收到的最大 seq」回来，服务端补发
    # 之后的。没有它就只能整份重来，或者放弃中间过程。
    seq: int | None = None

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
    # 任务语义投影（complete/incomplete）。消费方只读这个，不要枚举 end_reason：
    # 枚举式判断会让后端每新增一种终止原因，旧消费方就静默漏掉一种「没做完」。
    # 判定唯一入口：agentstream.outcome.derive_outcome。
    outcome: str | None = None
    # 完成证据等级（run 收尾时按**事实**判定，不读模型文本）：
    #   mutated=本轮内置写类工具成功执行；external=MCP 工具（能力未知）；
    #   metadata_only=只动了我们自己的元数据（如 plan_update）；
    #   read_only=仅只读工具；none=零工具调用。
    # UI 用它区分「模型结束了」和「已核实有产出」——两者不是一回事。
    completion_evidence: str | None = None
    # 计划快照：plan.updated 事件携带最新整份；run.completed 携带最终态。
    # 计划项标 done 必须绑定本轮真实工具调用，verified 由服务端核对后写入。
    plan: PlanSnapshot | None = None
    # 本轮耗时（毫秒）：run.started → 终态事件。仅终态事件携带，供 UI 展示"完成时长"
    duration_ms: int | None = None
