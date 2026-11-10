"""cronjob 的类型定义。

对应 Go 版 internal/cronjob/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

TASK_TYPE_ONCE = "once"
TASK_TYPE_RECURRING = "recurring"


class InvalidJob(Exception):
    pass


class JobNotFound(Exception):
    pass


@dataclass
class Job:
    """一条定时任务定义；执行结果保存在对应的 Agent 会话历史中。"""

    id: str
    clerk_user_id: str
    name: str
    task_type: str
    schedule: str
    prompt: str
    enabled: bool = True
    next_run_at: datetime | None = None
    running: bool = False
    created_at: datetime | None = None
    # 最近一次执行的结果。None = 还没跑过（区别于「跑过但成功」）。
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    # 连续失败次数：一次失败可能只是网络抖动，连续 N 次才是「这个任务已经坏了」。
    # 成功一次即清零，所以它是一个「当前是否健康」的指标，不是累计统计。
    consecutive_failures: int = 0


@dataclass
class JobInput:
    """创建或编辑任务时允许填写的字段。"""

    name: str
    task_type: str
    schedule: str
    prompt: str


class CreateRequest(BaseModel):
    """创建任务的 HTTP 输入；userId 由 BFF 注入。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    user_id: str = ""
    name: str = ""
    task_type: str = ""
    schedule: str = ""
    prompt: str = ""
    timezone: str = ""


class JobResponse(BaseModel):
    """返回给 Web 的安全任务表示。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: str
    task_type: str
    schedule: str
    prompt: str
    enabled: bool
    next_run_at: str | None = None
    running: bool
    created_at: str
    # null = 还没跑过；有值且 last_error 非空 = 最近一次失败了
    last_run_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    consecutive_failures: int = 0


class ListResponse(BaseModel):
    """任务列表 HTTP 输出。"""

    jobs: list[JobResponse]
