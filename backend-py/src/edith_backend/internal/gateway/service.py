"""gateway 服务：把渠道身份与会话翻译为 EDITH 身份后启动 AgentRun。

对应 Go 版 internal/gateway/service.go。
"""

from __future__ import annotations

import logging
import time

from ..agentrun.service import Service as AgentRunService
from ..agentrun.types import Error, Status, Stream
from ..userconfig.bindings import Bindings
from .types import CRON_CHANNEL, WEB_CHANNEL, IncomingMessage, to_agentrun_request

logger = logging.getLogger(__name__)


def _is_trusted_channel(channel: str) -> bool:
    return channel in (WEB_CHANNEL, CRON_CHANNEL)


class Service:
    """所有 Agent 渠道的唯一入口。"""

    def __init__(self, bindings: Bindings, agent_runs: AgentRunService) -> None:
        self.bindings = bindings
        self.agent_runs = agent_runs

    async def run(
        self, input_msg: IncomingMessage
    ) -> tuple[Stream | None, Error | None]:
        """把渠道身份与会话翻译为 EDITH 身份后启动 AgentRun。"""
        request_id = input_msg.request_id.strip()
        input_msg.channel = input_msg.channel.strip()
        input_msg.external_user_id = input_msg.external_user_id.strip()
        input_msg.session_key = input_msg.session_key.strip()
        input_msg.model_id = input_msg.model_id.strip()

        logger.info(
            "gateway_run_start request_id=%s channel=%s external_user_id=%s session_key=%s model_id=%s",
            request_id,
            input_msg.channel,
            input_msg.external_user_id,
            input_msg.session_key,
            input_msg.model_id,
        )

        start_time = time.monotonic()
        if not input_msg.channel or not input_msg.external_user_id:
            return None, Error(
                "invalid_request", "channel and externalUserId are required"
            )

        user_id = input_msg.external_user_id
        session_id = input_msg.session_key
        if not _is_trusted_channel(input_msg.channel):
            try:
                bound_user_id, found = await self.bindings.to_clerk_user_id(
                    input_msg.channel, input_msg.external_user_id
                )
            except ValueError as exc:
                return None, Error("internal_error", str(exc))
            if not found:
                return None, Error(
                    "identity_not_bound", "channel user is not bound to an EDITH user"
                )
            user_id = bound_user_id or ""
            session_id = f"{input_msg.channel}:{user_id}"
        if not session_id:
            return None, Error(
                "invalid_request", "sessionKey is required for trusted channels"
            )
        try:
            stream, run_error = await self.agent_runs.run(
                to_agentrun_request(input_msg, user_id, session_id)
            )
            duration_ms = (time.monotonic() - start_time) * 1000
            # 这里返回的是「已启动」，不是「已结束」：agent_runs.run 只负责准入
            # 与起 task，真正的执行结果在事件流里。老日志叫 gateway_run_end 并且
            # 无条件写 has_error=false，于是「准入就失败（如模型 402）」和
            # 「正常跑完」在日志里长得一模一样——一次连续 4 次失败就是这么被
            # 误读成「在跑」的。现在按返回值如实分岔。
            if run_error is not None:
                logger.warning(
                    "gateway_run_started request_id=%s duration_ms=%.2f has_error=true "
                    "error_type=%s error=%s",
                    request_id,
                    duration_ms,
                    run_error.type,
                    run_error.message,
                )
            else:
                logger.info(
                    "gateway_run_started request_id=%s duration_ms=%.2f has_error=false",
                    request_id,
                    duration_ms,
                )
            return stream, run_error
        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.exception(
                "gateway_run_started request_id=%s duration_ms=%.2f has_error=true error_type=%s",
                request_id,
                duration_ms,
                type(e).__name__,
            )
            raise

    def status(
        self, user_id: str, request_id: str
    ) -> tuple[Status | None, Error | None]:
        """查询一个属于可信 Clerk 用户的活跃任务。"""
        return self.agent_runs.status(user_id, request_id)

    def resume(
        self, user_id: str, request_id: str, after: int = 0
    ) -> tuple[Stream | None, Error | None]:
        """重新订阅一个已在运行（或刚结束）的任务的事件流。

        这是断流重连的入口。它只回放、不启动：刷新页面时前端的旧连接没了，
        但后端 run 还在跑、事件也都在日志里，带 `after=<已收到的最大 seq>`
        回来就能先补齐错过的部分，再无缝接上实时。
        """
        return self.agent_runs.subscribe(user_id, request_id, after)

    def cancel(self, user_id: str, request_id: str) -> Error | None:
        """停止一个属于可信 Clerk 用户的活跃任务。"""
        return self.agent_runs.cancel(user_id, request_id)


def new_service(bindings: Bindings, agent_runs: AgentRunService) -> Service:
    """创建 Gateway；它不会创建 AgentRun 或渠道 Adapter。"""
    if bindings is None or agent_runs is None:
        raise ValueError("gateway requires bindings and agent runs")
    return Service(bindings, agent_runs)
