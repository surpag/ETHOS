"""cronjob 调度器：周期扫描到点任务并原子抢占执行。

对应 Go 版 internal/cronjob/scheduler.go。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from .store import Store
from .types import Job

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 10


class JobRunner(Protocol):
    async def run_job(self, job: Job) -> None: ...


class Scheduler:
    """周期扫描到点任务；由 main 显式调用 run 启动。"""

    def __init__(self, store: Store, runner: JobRunner, interval: int = DEFAULT_POLL_INTERVAL) -> None:
        self._store = store
        self._runner = runner
        self._interval = interval

    async def run(self) -> None:
        try:
            await self._store.initialize_next_runs(datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            logger.warning("initialize cron next runs: %s", exc)
        while True:
            try:
                await self._schedule_due_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.warning("schedule due jobs: %s", exc)
            await asyncio.sleep(self._interval)

    async def _schedule_due_jobs(self) -> None:
        now = datetime.now(UTC)
        jobs = await self._store.due_jobs(now)
        for job in jobs:
            claimed = await self._store.claim_due(job.id, now)
            if claimed is None:
                continue
            asyncio.create_task(self._run_claimed(claimed))

    async def _run_claimed(self, job: Job) -> None:
        try:
            await self._runner.run_job(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("run cron job %s: %s", job.id, exc)
        try:
            await self._store.finish_run(job.id, datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            logger.warning("finish cron job %s: %s", job.id, exc)
