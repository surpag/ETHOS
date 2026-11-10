"""收尾阶段的附属调用与时间驱动的失败，必须都留下可核对的痕迹。

两件事放在一个文件里，因为它们是同一类问题的两个面：
    「没人看着的时候发生的事，必须在有人问起时能回答。」
- 自动记忆抽取是一次真实模型调用（花钱），不能不出现在账本上；
- 定时任务失败是用户看不见的（没有人在页面上等它），必须写进任务行。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiosqlite

from edith_backend.internal.cronjob import schema as cron_schema
from edith_backend.internal.cronjob.store import Store as CronStore
from edith_backend.internal.usage.store import Store as UsageStore
from edith_backend.internal.usage.types import Run, Tokens


async def _usage_store() -> tuple[UsageStore, aiosqlite.Connection]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    store = UsageStore(db)
    await store.create_schema()
    return store, db


async def _row(db: aiosqlite.Connection, request_id: str) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT * FROM usage_runs WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    return row


def test_auxiliary_tokens_are_added_to_the_same_run() -> None:
    async def go() -> None:
        store, db = await _usage_store()
        try:
            run = Run("r1", "u1", "s1", "m1")
            await store.start(run)
            await store.finish(
                run,
                Tokens(
                    prompt_tokens=100,
                    cached_prompt_tokens=40,
                    completion_tokens=10,
                    total_tokens=110,
                ),
            )
            await store.add_auxiliary(
                "r1",
                Tokens(
                    prompt_tokens=50,
                    cached_prompt_tokens=20,
                    completion_tokens=5,
                    total_tokens=55,
                ),
            )
            row = await _row(db, "r1")
            assert row["total_tokens"] == 165
            assert row["prompt_tokens"] == 150
            assert row["cached_prompt_tokens"] == 60
            assert row["cache_reported"] == 1
            # 关键：还是一次运行。附属调用不能把 total_runs 变成「模型调用次数」。
            summary = await store.session_summary("u1", "s1")
            assert summary.total_runs == 1
            assert summary.total_tokens == 165
        finally:
            await db.close()

    asyncio.run(go())


def test_auxiliary_without_cache_report_makes_row_unknown() -> None:
    """附属调用没上报缓存 → 整行缓存变未知，而不是把 None 当 0 加。

    把 None 当 0 求和会低估命中率，而「看起来精确的假数据」比缺数据更危险。
    """
    async def go() -> None:
        store, db = await _usage_store()
        try:
            run = Run("r2", "u1", "s1", "m1")
            await store.start(run)
            await store.finish(
                run,
                Tokens(
                    prompt_tokens=100,
                    cached_prompt_tokens=40,
                    completion_tokens=10,
                    total_tokens=110,
                ),
            )
            await store.add_auxiliary(
                "r2",
                Tokens(
                    prompt_tokens=50,
                    cached_prompt_tokens=None,
                    completion_tokens=5,
                    total_tokens=55,
                ),
            )
            row = await _row(db, "r2")
            assert row["total_tokens"] == 165
            assert row["cached_prompt_tokens"] is None
            assert row["cache_reported"] == 0
        finally:
            await db.close()

    asyncio.run(go())


# ---------------------------------------------------------------- 定时任务失败


async def _cron_store() -> tuple[CronStore, aiosqlite.Connection]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await cron_schema.create_schema(db)
    return CronStore(db, settings=None), db


async def _insert_job(db: aiosqlite.Connection, job_id: str) -> None:
    await db.execute(
        "INSERT INTO cron_jobs (id, clerk_user_id, name, task_type, schedule, prompt, "
        "enabled, running, created_at) VALUES (?, 'u1', '日报', 'once', '2026-11-05T00:00:00Z', "
        "'写日报', 1, 1, '2026-11-04T00:00:00Z')",
        (job_id,),
    )
    await db.commit()


def test_cron_failure_is_recorded_and_counted() -> None:
    async def go() -> None:
        store, db = await _cron_store()
        try:
            await _insert_job(db, "j1")
            now = datetime(2026, 11, 4, 1, tzinfo=UTC)
            await store.finish_run("j1", now, "RuntimeError: 402 insufficient balance")
            job = await store._job("j1")
            assert job.last_error is not None
            assert "402" in job.last_error
            assert job.consecutive_failures == 1
            assert job.last_run_at is not None
            assert job.running is False

            # 第二次仍失败：连续次数要累加（一次是抖动，连续才是「坏了」）
            await db.execute("UPDATE cron_jobs SET running = 1 WHERE id = 'j1'")
            await db.commit()
            await store.finish_run("j1", now, "RuntimeError: 402")
            job = await store._job("j1")
            assert job.consecutive_failures == 2
        finally:
            await db.close()

    asyncio.run(go())


def test_cron_success_clears_previous_failure() -> None:
    """成功一次就清零：这个计数表达的是「当前是否健康」，不是累计统计。"""

    async def go() -> None:
        store, db = await _cron_store()
        try:
            await _insert_job(db, "j2")
            now = datetime(2026, 11, 4, 1, tzinfo=UTC)
            await store.finish_run("j2", now, "RuntimeError: boom")
            await db.execute("UPDATE cron_jobs SET running = 1 WHERE id = 'j2'")
            await db.commit()
            await store.finish_run("j2", now)
            job = await store._job("j2")
            assert job.last_error is None
            assert job.last_error_at is None
            assert job.consecutive_failures == 0
        finally:
            await db.close()

    asyncio.run(go())


def test_cron_schema_migration_is_idempotent() -> None:
    """老库补列必须幂等：每次启动都会跑一遍，重复加列会直接启动失败。"""

    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        try:
            # 模拟旧版表（没有结果列）
            await db.execute(
                """CREATE TABLE cron_jobs (
                    id TEXT PRIMARY KEY, clerk_user_id TEXT NOT NULL, name TEXT NOT NULL,
                    task_type TEXT NOT NULL, schedule TEXT NOT NULL, prompt TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT,
                    running INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"""
            )
            await db.commit()
            await cron_schema.create_schema(db)
            await cron_schema.create_schema(db)
            cursor = await db.execute("PRAGMA table_info(cron_jobs)")
            columns = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            assert {"last_error", "last_error_at", "last_run_at", "consecutive_failures"} <= columns
        finally:
            await db.close()

    asyncio.run(go())
