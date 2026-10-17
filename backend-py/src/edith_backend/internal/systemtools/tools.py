"""系统工具集：get_current_time。

对应 Go 版 internal/systemtools/current_time.go。
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


class SystemToolSet:
    def tool_definitions(self) -> list[dict]:
        return [
            _tool(
                "get_current_time",
                "获取指定时区的当前时间。用户询问现在几点、某地时间或需要比较时区时使用。",
                {"timezone": {"type": "string", "description": "时区，例如 Asia/Shanghai"}},
                [],
            )
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",  # 仅接口兼容（ToolRegistry 统一透传），本工具集不使用
    ) -> str:
        if name != "get_current_time":
            return json.dumps({"error": f"unknown system tool: {name}"}, ensure_ascii=False)
        timezone = arguments.get("timezone") or "Asia/Shanghai"
        try:
            zone = ZoneInfo(timezone)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"unknown timezone {timezone}: {exc}"}, ensure_ascii=False)
        now = datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S")
        return json.dumps({"time": now, "timezone": timezone}, ensure_ascii=False)
