"""cronjob 的 SQLite 访问器：CRUD 与调度抢占。

对应 Go 版 internal/cronjob/store.go + schedule_store.go。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite

from ..userconfig.settings import Settings
from . import schedule_rules
from .types import (
    TASK_TYPE_ONCE,
    TASK_TYPE_RECURRING,
    InvalidJob,
    Job,
    JobInput,
    JobNotFound,
)

_COLUMNS = "id, clerk_user_id, name, task_type, schedule, prompt, enabled, next_run_at, running, created_at"


def _dt(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_job(row) -> Job:
    return Job(
        id=row["id"],
        clerk_user_id=row["clerk_user_id"],
        name=row["name"],
        task_type=row["task_type"],
        schedule=row["schedule"],
        prompt=row["prompt"],
        enabled=bool(row["enabled"]),
        next_run_at=_parse_dt(row["next_run_at"]),
        running=bool(row["running"]),
        created_at=_parse_dt(row["created_at"]),
    )


class Store:
    def __init__(self, db: aiosqlite.Connection, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def create(self, user_id: str, input_: JobInput) -> Job:
        user_id = user_id.strip()
        if not user_id:
            raise InvalidJob("user ID is required")
        schedule_rules.validate_input(input_)
        now = datetime.now(UTC)
        next_at = await schedule_rules.next_run(self._settings, user_id, input_, now)
        job = Job(
            id=uuid4().hex,
            clerk_user_id=user_id,
            name=input_.name.strip(),
            task_type=input_.task_type,
            schedule=input_.schedule.strip(),
            prompt=input_.prompt.strip(),
            enabled=True,
            next_run_at=next_at,
            created_at=now,
        )
        await self._db.execute(
            "INSERT INTO cron_jobs (id, clerk_user_id, name, task_type, schedule, prompt, enabled, next_run_at, running, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?)",
            (job.id, job.clerk_user_id, job.name, job.task_type, job.schedule, job.prompt,
             _dt(job.next_run_at), _dt(job.created_at)),
        )
        await self._db.commit()
        return job

    async def list(self, user_id: str) -> list[Job]:
        cursor = await self._db.execute(
            f"SELECT {_COLUMNS} FROM cron_jobs WHERE clerk_user_id = ? ORDER BY created_at, id",
            (user_id.strip(),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_job(row) for row in rows]

    async def update(self, user_id: str, job_id: str, input_: JobInput) -> Job:
        schedule_rules.validate_input(input_)
        next_at = await schedule_rules.next_run(self._settings, user_id.strip(), input_, datetime.now(UTC))
        cursor = await self._db.execute(
            "UPDATE cron_jobs SET name = ?, task_type = ?, schedule = ?, prompt = ?, next_run_at = ? "
            "WHERE id = ? AND clerk_user_id = ?",
            (input_.name.strip(), input_.task_type, input_.schedule.strip(), input_.prompt.strip(),
             _dt(next_at), job_id.strip(), user_id.strip()),
        )
        if cursor.rowcount != 1:
            raise JobNotFound("cron job not found")
        await self._db.commit()
        return await self._job(job_id)

    async def delete(self, user_id: str, job_id: str) -> None:
        cursor = await self._db.execute(
            "DELETE FROM cron_jobs WHERE id = ? AND clerk_user_id = ?",
            (job_id.strip(), user_id.strip()),
        )
        if cursor.rowcount != 1:
            raise JobNotFound("cron job not found")
        await self._db.commit()

    async def set_enabled(self, user_id: str, job_id: str, enabled: bool) -> Job:
        current = await self._job_for_user(user_id, job_id)
        next_at = current.next_run_at
        if enabled and next_at is None:
            next_at = await schedule_rules.next_run(
                self._settings, current.clerk_user_id,
                JobInput(current.name, current.task_type, current.schedule, current.prompt),
                datetime.now(UTC),
            )
        await self._db.execute(
            "UPDATE cron_jobs SET enabled = ?, next_run_at = ? WHERE id = ? AND clerk_user_id = ?",
            (1 if enabled else 0, _dt(next_at), current.id, current.clerk_user_id),
        )
        await self._db.commit()
        return await self._job(current.id)

    async def _job_for_user(self, user_id: str, job_id: str) -> Job:
        cursor = await self._db.execute(
            f"SELECT {_COLUMNS} FROM cron_jobs WHERE id = ? AND clerk_user_id = ?",
            (job_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise JobNotFound("cron job not found")
        return _row_to_job(row)

    async def _job(self, job_id: str) -> Job:
        cursor = await self._db.execute(f"SELECT {_COLUMNS} FROM cron_jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise JobNotFound("cron job not found")
        return _row_to_job(row)

    # ---- 调度相关 ----

    async def due_jobs(self, now: datetime) -> list[Job]:
        cursor = await self._db.execute(
            f"SELECT {_COLUMNS} FROM cron_jobs WHERE enabled = 1 AND running = 0 "
            "AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at",
            (_dt(now),),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_row_to_job(row) for row in rows]

    async def claim_due(self, job_id: str, now: datetime) -> Job | None:
        cursor = await self._db.execute(
            "UPDATE cron_jobs SET running = 1 WHERE id = ? AND enabled = 1 AND running = 0 "
            "AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (job_id, _dt(now)),
        )
        if cursor.rowcount != 1:
            return None
        await self._db.commit()
        return await self._job(job_id)

    async def finish_run(self, job_id: str, now: datetime) -> None:
        job = await self._job(job_id)
        next_at = None
        if job.task_type == TASK_TYPE_RECURRING:
            next_at = await schedule_rules.next_run(
                self._settings, job.clerk_user_id,
                JobInput(job.name, job.task_type, job.schedule, job.prompt), now,
            )
        await self._db.execute(
            "UPDATE cron_jobs SET running = 0, next_run_at = ?, "
            "enabled = CASE WHEN task_type = ? THEN 0 ELSE enabled END WHERE id = ?",
            (_dt(next_at), TASK_TYPE_ONCE, job_id),
        )
        await self._db.commit()

    async def initialize_next_runs(self, now: datetime) -> None:
        cursor = await self._db.execute(
            f"SELECT {_COLUMNS} FROM cron_jobs WHERE enabled = 1 AND next_run_at IS NULL"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        jobs = [_row_to_job(row) for row in rows]
        for job in jobs:
            next_at = await schedule_rules.next_run(
                self._settings, job.clerk_user_id,
                JobInput(job.name, job.task_type, job.schedule, job.prompt), now,
            )
            await self._db.execute(
                "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (_dt(next_at), job.id)
            )
        if jobs:
            await self._db.commit()
