"""plan_update 工具：让「计划」成为 agent 自己维护的一等状态。

为什么这必须是工具而不是提示词里的一句「请先列计划」：
    提示词里的计划只是一次性的文本，下一轮就被压缩改写；而工具调用是**结构化
    且可校验**的——服务端能拒绝它，也能核对它。这正是它比「一段计划文字」强
    的地方，也是它唯一的价值来源：不可校验的计划只是把「假完成」从一句话升级
    成一个看起来更可信的打勾列表。
"""

from __future__ import annotations

import json
import logging

from ..session.plan_store import RunPlanStore
from .run_plan import (
    MAX_PLAN_ITEMS,
    MODE_SET,
    MODE_UPDATE,
    PLAN_STATUSES,
    RunPlanRegistry,
)

logger = logging.getLogger(__name__)

_PLAN_UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "plan_update",
        "description": (
            "维护当前任务的执行计划。任务超过 3 步时先用 mode=set 提交完整计划，"
            "之后每推进一步用 mode=update 只更新变化的项。\n"
            "把某一项标为 done 时**必须**带 evidence，说明「什么能证明这一步做完了」："
            "改了文件就给 {\"path\":\"相对路径\"}，跑通了命令就给 {\"expect_exit_code\":0}。"
            "不需要填 tool_call_ids——服务端会自己在本轮真实的工具调用里找对应记录并核对，"
            "找不到就拒绝这次更新（failures 里会列出本轮已成功的调用供你修正）。"
            "没做成的项请标 blocked 并说明原因，不要为了好看标 done。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": [MODE_SET, MODE_UPDATE],
                    "description": "set=提交/覆盖完整计划；update=只改指定项的 status",
                },
                "items": {
                    "type": "array",
                    "description": f"计划项，最多 {MAX_PLAN_ITEMS} 项",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "稳定标识（同一计划内唯一），update 时用它定位",
                            },
                            "title": {
                                "type": "string",
                                "description": "一句短动作，例如「实现 SudokuSolver.solve」",
                            },
                            "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                            "evidence": {
                                "type": "object",
                                "description": "标 done 时必填：说明这一步的可核对证据",
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "description": "文件类证据：被改动的相对路径",
                                    },
                                    "expect_exit_code": {
                                        "type": "integer",
                                        "description": "命令类证据：期望的退出码（通常 0）",
                                    },
                                    "tool_call_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "可选，一般不用填：服务端会自动匹配",
                                    },
                                },
                                "required": [],
                            },
                        },
                        "required": ["id", "status"],
                    },
                },
            },
            "required": ["mode", "items"],
        },
    },
}


def _error(message: str, failures: list[str] | None = None, kind: str = "verification_failed") -> str:
    payload: dict = {
        "error": message,
        "kind": kind,
        # 业务性拒绝：模型可以据此修正后重试，不参与工具熔断
        "retryable": True,
    }
    if failures:
        payload["failures"] = failures
        payload["suggestion"] = (
            "请按上面每条原因修正后重新提交：evidence 用 path 或 expect_exit_code 声明"
            "「什么算做完」，服务端会在本轮已成功的调用里自己找对应记录；"
            "还没做成的项请标 in_progress 或 blocked，不要标 done。"
        )
    return json.dumps(payload, ensure_ascii=False)


class PlanToolSet:
    """计划工具的集合（当前只有 plan_update 一个）。"""

    def __init__(self, plans: RunPlanRegistry, store: RunPlanStore) -> None:
        self._plans = plans
        self._store = store

    def tool_definitions(self) -> list[dict]:
        return [_PLAN_UPDATE_TOOL]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",  # 仅接口兼容（ToolRegistry 统一透传），计划与工作区无关
    ) -> str:
        if name != "plan_update":
            return json.dumps(
                {"error": f"unknown plan tool: {name}"}, ensure_ascii=False
            )

        plan = self._plans.current(user_id, session_id)
        if plan is None:
            # 正常情况下不会发生：run 启动时就会 open。出现即说明没有活跃 run，
            # 让模型停止调用而不是空转。
            return _error(
                "当前没有正在执行的任务，无法更新计划",
                kind="command_error",
            )

        mode = str(arguments.get("mode") or "").strip()
        items = arguments.get("items") or []
        ok, failures, summary = plan.apply(mode, items)
        if not ok:
            logger.info(
                "plan_update_rejected request_id=%s mode=%s failures=%d first=%s",
                plan.request_id,
                mode,
                len(failures),
                failures[0][:160] if failures else "-",
            )
            return _error("计划更新被拒绝", failures)

        # 落库失败不该让模型的重试循环卡在这里：内存态与事件已经是对的，
        # 代价只是「刷新后看不到这一版」。但必须留下痕迹。
        try:
            await self._store.save(
                plan.user_id, plan.session_id, plan.request_id, plan.revision, [
                    item.to_dict() for item in plan.items
                ]
            )
        except Exception:
            logger.exception(
                "plan_save_failed request_id=%s revision=%d",
                plan.request_id,
                plan.revision,
            )

        self._emit(plan)

        return json.dumps(
            {
                "ok": True,
                "progress": summary,
                "items": [
                    {"id": item.id, "status": item.status, "verified": item.verified}
                    for item in plan.items
                ],
            },
            ensure_ascii=False,
        )

    def _emit(self, plan) -> None:
        """把最新快照推给前端。发射失败（队列已关闭等）不影响工具语义。"""
        if plan.emit is None:
            return
        from ..agentstream.types import Event
        from .run_plan import to_snapshot

        try:
            plan.emit(
                Event(
                    type="plan.updated",
                    request_id=plan.request_id,
                    session_id=plan.session_id,
                    plan=to_snapshot(plan),
                )
            )
        except Exception:
            logger.exception("plan_emit_failed request_id=%s", plan.request_id)
