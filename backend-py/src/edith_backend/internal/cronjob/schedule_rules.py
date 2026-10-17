"""cronjob 的调度规则：校验输入与计算下次执行时间。

对应 Go 版 internal/cronjob/schedule_rules.go。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from .types import TASK_TYPE_ONCE, TASK_TYPE_RECURRING, InvalidJob, JobInput

_DEFAULT_TIMEZONE = "Asia/Shanghai"


def validate_input(input_: JobInput) -> None:
    if not input_.name.strip() or not input_.prompt.strip() or not input_.schedule.strip():
        raise InvalidJob("name, schedule and prompt are required")
    if input_.task_type not in (TASK_TYPE_ONCE, TASK_TYPE_RECURRING):
        raise InvalidJob("task type must be once or recurring")


async def next_run(settings, user_id: str, input_: JobInput, from_time: datetime) -> datetime:
    """按任务类型和用户时区计算下一次执行时间。"""
    if input_.task_type == TASK_TYPE_ONCE:
        try:
            return datetime.fromisoformat(input_.schedule.strip())
        except ValueError:
            raise InvalidJob("once schedule must be RFC3339")

    zone = await settings.load_timezone(user_id)
    zone = zone.strip() or _DEFAULT_TIMEZONE
    try:
        location = ZoneInfo(zone)
    except Exception:  # noqa: BLE001 - 任意无效时区都归为 InvalidJob
        raise InvalidJob(f'unknown timezone "{zone}"')
    localized = from_time.astimezone(location)
    try:
        iterator = croniter(input_.schedule.strip(), localized)
        return iterator.get_next(datetime)
    except Exception:  # noqa: BLE001 - 任意无效 cron 表达式都归为 InvalidJob
        raise InvalidJob("invalid cron expression")
