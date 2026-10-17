"""cronjob 的 Agent 工具：create_cron_job。

对应 Go 版 internal/cronjob/tools.go。
"""

from __future__ import annotations

import json
from datetime import UTC

from .store import Store
from .types import JobInput


def tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "create_cron_job",
            "description": "创建当前用户的定时任务。只能填写任务名称、类型、执行时间和任务指令；用户身份由当前 Runner 会话自动确定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名称，简短描述任务用途"},
                    "taskType": {"type": "string", "description": "任务类型：once 或 recurring"},
                    "schedule": {"type": "string", "description": "一次性任务填写 RFC3339 时间；周期任务填写五段 cron 表达式"},
                    "prompt": {"type": "string", "description": "到点后交给 Agent 执行的指令"},
                },
                "required": ["name", "taskType", "schedule", "prompt"],
            },
        },
    }


class CronToolSet:
    def __init__(self, store: Store) -> None:
        self._store = store

    def tool_definitions(self) -> list[dict]:
        return [tool_definition()]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",  # 仅接口兼容（ToolRegistry 统一透传），本工具集不使用
    ) -> str:
        if name != "create_cron_job":
            return json.dumps({"error": f"unknown cron tool: {name}"}, ensure_ascii=False)
        try:
            job = await self._store.create(
                user_id,
                JobInput(
                    name=arguments.get("name", ""),
                    task_type=arguments.get("taskType", ""),
                    schedule=arguments.get("schedule", ""),
                    prompt=arguments.get("prompt", ""),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        next_at = job.next_run_at.astimezone(UTC).isoformat() if job.next_run_at else ""
        return json.dumps(
            {"id": job.id, "name": job.name, "taskType": job.task_type,
             "schedule": job.schedule, "nextRunAt": next_at, "message": "定时任务已创建"},
            ensure_ascii=False,
        )
