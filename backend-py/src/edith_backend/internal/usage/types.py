"""usage 模块的类型定义。

对应 Go 版 internal/usage/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


@dataclass
class Run:
    """一次 Agent 运行的稳定身份。"""

    request_id: str
    user_id: str
    session_id: str
    model_id: str


@dataclass
class Tokens:
    """一次运行累计的供应商 Token 用量。

    reported=False 表示这些数字是本地估算（供应商流里没返回 usage）。
    估算值也计入总量，但必须可与真实上报区分，否则「看起来精确的假数据」
    比缺数据更危险。
    """

    prompt_tokens: int = 0
    cached_prompt_tokens: int | None = None
    completion_tokens: int = 0
    total_tokens: int = 0
    reported: bool = True


class Summary(BaseModel):
    """一个会话的浏览器可展示用量汇总。

    缓存类字段按「已上报缓存的全集子集」聚合：只要有一次运行没上报，
    就只统计上报过的那些运行，并用 cache_reported_runs/total_runs 说明覆盖度，
    而不是整会话一起变成未知（旧实现的全有或全无语义会让一次估算废掉整个会话）。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    total_tokens: int
    cached_prompt_tokens: int | None = None
    uncached_prompt_tokens: int | None = None
    completion_tokens: int
    cache_hit_rate: float | None = None
    # 覆盖度：total_runs = 计入统计的已完成运行数；
    # cache_reported_runs = 其中真实上报了缓存字段的运行数；
    # estimated_runs = 其中用量由本地估算的运行数（不计缓存）。
    total_runs: int = 0
    cache_reported_runs: int = 0
    estimated_runs: int = 0
