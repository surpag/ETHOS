"""cronjob 的 HTTP 管理接口。

对应 Go 版 internal/cronjob/http.go + routes.go。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..http_utils import EDITHError
from ..userconfig.settings import Settings
from .store import Store
from .types import (
    CreateRequest,
    InvalidJob,
    Job,
    JobInput,
    JobNotFound,
    JobResponse,
    ListResponse,
)


def _job_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        name=job.name,
        task_type=job.task_type,
        schedule=job.schedule,
        prompt=job.prompt,
        enabled=job.enabled,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        running=job.running,
        created_at=job.created_at.isoformat() if job.created_at else "",
    )


class HTTP:
    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/cron-jobs", response_model=ListResponse)
        async def list_jobs(userId: str = "") -> ListResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            jobs = await self._store.list(userId)
            return ListResponse(jobs=[_job_response(j) for j in jobs])

        @router.post("/internal/cron-jobs", response_model=JobResponse)
        async def create_job(body: CreateRequest) -> JobResponse:
            if body.timezone.strip():
                await self._settings.save_timezone(body.user_id, body.timezone)
            try:
                job = await self._store.create(body.user_id, JobInput(body.name, body.task_type, body.schedule, body.prompt))
            except InvalidJob as exc:
                raise EDITHError(400, "invalid_cron_job", str(exc))
            return _job_response(job)

        @router.put("/internal/cron-jobs/{job_id}", response_model=JobResponse)
        async def update_job(job_id: str, body: CreateRequest) -> JobResponse:
            try:
                job = await self._store.update(body.user_id, job_id, JobInput(body.name, body.task_type, body.schedule, body.prompt))
            except InvalidJob as exc:
                raise EDITHError(400, "invalid_cron_job", str(exc))
            except JobNotFound:
                raise EDITHError(404, "cron_job_not_found", "定时任务不存在")
            return _job_response(job)

        @router.delete("/internal/cron-jobs/{job_id}", status_code=204)
        async def delete_job(job_id: str, userId: str = "") -> None:
            try:
                await self._store.delete(userId, job_id)
            except JobNotFound:
                raise EDITHError(404, "cron_job_not_found", "定时任务不存在")

        @router.post("/internal/cron-jobs/{job_id}/toggle", response_model=JobResponse)
        async def toggle_job(job_id: str, userId: str = "", enabled: str = "false") -> JobResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            try:
                job = await self._store.set_enabled(userId, job_id, enabled == "true")
            except JobNotFound:
                raise EDITHError(404, "cron_job_not_found", "定时任务不存在")
            return _job_response(job)

        return router
