"""cronadapter：把定时任务转换为 Gateway 消息并消费结果。

对应 Go 版 internal/cronadapter/adapter.go。
"""

from __future__ import annotations

from uuid import uuid4

from ..cronjob.types import Job
from ..gateway.service import Service as GatewayService
from ..gateway.types import CRON_CHANNEL, IncomingMessage


class Adapter:
    """Cron 渠道执行器；不负责轮询和任务存储。"""

    def __init__(self, gateway: GatewayService) -> None:
        self._gateway = gateway

    async def run_job(self, job: Job) -> None:
        """把一个已抢占任务交给 Gateway，并消费事件直到 AgentRun 收尾。"""
        stream, run_error = await self._gateway.run(
            IncomingMessage(
                channel=CRON_CHANNEL,
                external_user_id=job.clerk_user_id,
                session_key=f"cron:{job.id}",
                request_id=uuid4().hex,
                message=job.prompt,
            )
        )
        if run_error is not None:
            raise RuntimeError(f"start cron agent run: {run_error.message}")
        execution_error: Exception | None = None
        while True:
            event = await stream.events.get()
            if event is None:
                break
            if event.type == "run.error" and event.error is not None:
                execution_error = RuntimeError(event.error.message)
        if execution_error is not None:
            raise execution_error
