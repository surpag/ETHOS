"""run 级任务计划：状态机、证据校验、活跃计划注册表。

这个模块存在的唯一理由，是「计划」本身会变成一种更危险的假象：
    把「模型说完成了」升级成「一个打勾的列表」，看起来更可信，**但同样可能没做**。
所以计划项标 done 时必须绑定证据，而证据要能在服务端被核对——这不是提示词
能解决的问题，必须是结构问题。

三条不可让渡的规则：
    1. **done 必须有证据**：没有可核对的工具调用，就不允许标完成。模型做不出
       证据时，正确的表达是 in_progress / blocked，而不是打勾。
    2. **只认事实，不读文本**：证据是「改了哪个路径 / 命令退出码是多少」，由服务端
       在本 run 的工具调用记录里自己找对应记录。任何「从回复里读『已完成』」的做法
       都会误报，而且随话术变化立刻失效。
    3. **整批拒绝，不部分生效**：只要有一项校验不过，这次更新整体不落地。
       部分生效会让计划与现实错位，比直接拒绝更难排查。

关于「业务错误也算 completed」的既有坑：
    execution.py 里 `status = "failed" if retryable is False else "completed"`，
    所以 `FileNotFoundError` 这类业务错误（retryable=True）会被记成 completed。
    因此判断一次调用是否真的成功，**必须同时看 result 里的 error 字段**，
    只看 status 会把失败当成功——这正是「假完成」的原产地。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 计划项状态。刻意只给四档：多一档就多一种「模型自行发明语义」的机会。
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
PLAN_STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED)

MODE_SET = "set"
MODE_UPDATE = "update"

# 上限不是审美：计划本质是「往上下文里塞常驻文本」，条数越多模型对每条的
# 遵从率越低。下限刻意取 1 而不是 3——硬性最少 3 条只会让两步任务凑数，
# 而凑数项就是下一轮「假完成」的温床。
MAX_PLAN_ITEMS = 8
MAX_ITEM_TITLE_CHARS = 80


def _now() -> str:
    # 统一用 UTC：库里其他时间列都是 UTC（CURRENT_TIMESTAMP），别让计划快照
    # 成为唯一一个本地时区的时间戳。
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_path(path: str) -> str:
    text = (path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _result_dict(result: str) -> dict:
    try:
        data = json.loads(result) if result else None
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _exit_code(data: dict) -> int | None:
    """命令退出码。sandbox_run_command 返回的是 camelCase `exitCode`。

    这里同时兼容 `exit_code`：不同工具/历史版本用的键不一致，而「取不到退出码」
    会让所有命令类证据静默失效——线上第一次跑就踩到了（全部返回 None，导致
    合法证据被拒），所以读取集中在这一个函数里，不要各处再写一遍。
    """
    for key in ("exitCode", "exit_code"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def _is_successful(fact: dict) -> bool:
    """一次调用是否真的成功了。

    三个条件缺一不可（顺序对应三种真实故障）：
      1. status=completed —— 工具层没把它判成系统故障；
      2. 结果里没有 error —— 业务错误（文件不存在等）在记录里仍是 completed；
      3. 命令类退出码为 0 / 未上报 —— 非零退出是业务失败，但它不带 error 字段。
    """
    if fact.get("status") != "completed":
        return False
    data = _result_dict(fact.get("result", ""))
    if data.get("error"):
        return False
    if str(fact.get("name", "")) == _RUN_COMMAND:
        code = _exit_code(data)
        if code not in (None, 0):
            return False
    return True


# 只读工具不能当「改动了什么」的证据（与 execution._READ_ONLY_TOOLS 同义，
# 但这里不能 import execution：会形成 agentrun 内部环）。
_READ_ONLY_TOOLS = frozenset({"sandbox_read_file", "sandbox_list_files"})
_RUN_COMMAND = "sandbox_run_command"


def _summarize_calls(calls: list[dict], limit: int = 5) -> str:
    """把本轮成功的调用压成一句清单，作为拒绝时的可执行提示。

    这一句是让模型能自己修正的关键：它不知道 tool_call_id 长什么样，但完全知道
    自己刚写过哪个路径、跑过哪条命令。所以拒绝信息必须回到**它能理解的语言**上。
    """
    parts = []
    for call in calls[:limit]:
        args = _result_dict(call.get("arguments", "")) or {}
        hint = args.get("path") or args.get("command") or ""
        parts.append(f"{call.get('name', '?')}({hint})" if hint else str(call.get("name", "?")))
    suffix = " …" if len(calls) > limit else ""
    return "; ".join(parts) + suffix


def resolve_evidence(evidence: dict, facts: list[dict]) -> tuple[dict | None, list[str]]:
    """把模型声明的证据解析成本轮真实的工具调用，返回 (解析结果, 失败原因)。

    为什么不让模型直接报 tool_call_id：
        那是服务端生成的不透明字符串，模型既看不到也记不住。真实后果是它宁愿
        一直不改状态，也不敢标 done——计划就废了。所以契约改成「模型声明什么算
        证明（路径 / 退出码），服务端自己去找哪次调用构成了证明」。

    支持的声明：
        文件类：{"path": "src/main.py"}           —— 本轮有一次成功的改动型调用涉及该路径
        命令类：{"expect_exit_code": 0}          —— 本轮有一次成功的命令退出码相符
        两者可同时给；给了 tool_call_ids 时会进一步限定候选范围（可选）。
    """
    if not isinstance(evidence, dict):
        return None, [
            'evidence 必须是对象，例如 {"path": "src/a.py"} 或 {"expect_exit_code": 0}'
        ]

    ok_calls = [f for f in facts if f.get("id") and _is_successful(f)]
    if not facts:
        return None, ["本轮还没有任何已完成的工具调用：请先执行动作，再更新计划"]

    failures: list[str] = []

    # 可选：模型主动限定候选（一般不需要，给出来就严格核对）
    ids = evidence.get("tool_call_ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            return None, ["tool_call_ids 给了就必须是非空数组；不确定可以整个省略"]
        by_id = {f.get("id"): f for f in facts if f.get("id")}
        missing = [i for i in ids if i not in by_id]
        if missing:
            return None, [
                (
                    f"以下 tool_call_id 不在本轮的工具调用里：{missing}。"
                    "通常不需要填这个字段——直接给 path 或 expect_exit_code 即可"
                )
            ]
        unusable = [f.get("id") for f in (by_id[i] for i in ids) if not _is_successful(f)]
        if unusable:
            return None, [
                f"以下调用并未成功执行：{unusable}。失败的调用不能作为完成证据"
            ]
        pool = [by_id[i] for i in ids]
    else:
        pool = ok_calls

    resolved_ids: list[str] = []

    path = evidence.get("path")
    if isinstance(path, str) and path.strip():
        target = _normalize_path(path)
        # 只读工具即使参数里带路径也只证明「读过」，不证明「改过」
        matched = [
            f
            for f in pool
            if str(f.get("name", "")) not in _READ_ONLY_TOOLS
            and target
            and target in _normalize_path(str(f.get("arguments", "")))
        ]
        if matched:
            resolved_ids.extend(str(f["id"]) for f in matched)
        else:
            failures.append(
                f"没有找到「改动过 {path}」的调用。本轮已成功的调用："
                f"{_summarize_calls(ok_calls)}。只读调用不能证明文件被修改过"
            )

    expect = evidence.get("expect_exit_code")
    if isinstance(expect, int):
        # 诊断信息用**全部**命令调用（含失败的）：告诉模型「本轮命令退出码：无」
        # 等于隐瞒它刚跑过一条失败命令，它只会原地重试同一条命令。
        all_commands = [f for f in facts if str(f.get("name", "")) == _RUN_COMMAND]
        commands = [f for f in pool if str(f.get("name", "")) == _RUN_COMMAND]
        matched_cmd = [
            f for f in commands if _exit_code(_result_dict(f.get("result", ""))) == expect
        ]
        if matched_cmd:
            resolved_ids.extend(str(f["id"]) for f in matched_cmd)
        else:
            codes = [_exit_code(_result_dict(f.get("result", ""))) for f in all_commands]
            failures.append(
                f"没有找到退出码为 {expect} 的成功命令（本轮命令退出码：{codes or '无'}）"
            )

    if not (isinstance(path, str) and path.strip()) and not isinstance(expect, int):
        return None, [
            (
                "evidence 太空泛：请给出 path（改了哪个文件）或 expect_exit_code（命令是否成功），"
                "否则无法核对"
            )
        ]

    if failures:
        return None, failures

    resolved: dict = {"tool_call_ids": sorted(set(resolved_ids))}
    if isinstance(path, str) and path.strip():
        resolved["path"] = path
    if isinstance(expect, int):
        resolved["expect_exit_code"] = expect
    return resolved, []


@dataclass
class PlanItem:
    id: str
    title: str
    status: str = STATUS_PENDING
    evidence: dict | None = None
    # verified=True 只可能来自服务端校验通过；None = 未标完成或未提供证据
    verified: bool | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "evidence": self.evidence,
            "verified": self.verified,
        }


@dataclass
class RunPlan:
    """一次 run 的计划状态。facts 是**引用**本 run 的 tool_call_records（同一对象）。"""

    request_id: str
    user_id: str
    session_id: str
    facts: list[dict] = field(default_factory=list)
    emit: Callable[[Any], None] | None = None
    revision: int = 0
    items: list[PlanItem] = field(default_factory=list)
    updated_at: str = ""

    # ── 校验 ──────────────────────────────────────────────────────
    def _validate_set(self, raw_items: list) -> tuple[list[PlanItem], list[str]]:
        if not isinstance(raw_items, list) or not raw_items:
            return [], ["mode=set 需要 items 数组，且不能为空"]
        if len(raw_items) > MAX_PLAN_ITEMS:
            return [], [
                (
                    f"计划最多 {MAX_PLAN_ITEMS} 项，当前 {len(raw_items)} 项："
                    "计划是常驻上下文，条目越多每条越容易被忽略"
                )
            ]
        parsed: list[PlanItem] = []
        seen: set[str] = set()
        failures: list[str] = []
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                failures.append(f"第 {index} 项必须是对象")
                continue
            item_id = str(raw.get("id") or "").strip() or str(index)
            if item_id in seen:
                failures.append(f"计划项 id 重复：{item_id}")
            seen.add(item_id)
            title = str(raw.get("title") or "").strip()
            if not title:
                failures.append(f"第 {index} 项缺少 title")
            elif len(title) > MAX_ITEM_TITLE_CHARS:
                failures.append(
                    f"第 {index} 项 title 过长（{len(title)} > {MAX_ITEM_TITLE_CHARS}）：请写成一句短动作"
                )
            status = str(raw.get("status") or STATUS_PENDING).strip()
            if status not in PLAN_STATUSES:
                failures.append(
                    f"第 {index} 项 status 非法：{status}（只允许 {'/'.join(PLAN_STATUSES)}）"
                )
            parsed.append(
                PlanItem(id=item_id, title=title, status=status, evidence=raw.get("evidence"))
            )
        return parsed, failures

    def _validate_update(self, raw_items: list) -> tuple[list[PlanItem], list[str]]:
        if not isinstance(raw_items, list) or not raw_items:
            return [], ["mode=update 需要 items 数组，且不能为空"]
        known = {item.id: item for item in self.items}
        if not known:
            return [], ["尚未设置计划：请先用 mode=set 提交完整计划"]
        failures: list[str] = []
        merged: dict[str, PlanItem] = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                failures.append("items 里每项必须是对象")
                continue
            item_id = str(raw.get("id") or "").strip()
            if item_id not in known:
                failures.append(
                    f"未知计划项 id：{item_id}（现有 id：{sorted(known)}）"
                )
                continue
            status = str(raw.get("status") or "").strip()
            if status not in PLAN_STATUSES:
                failures.append(
                    f"计划项 {item_id} 的 status 非法：{status}（只允许 {'/'.join(PLAN_STATUSES)}）"
                )
                continue
            base = merged.get(item_id) or known[item_id]
            merged[item_id] = PlanItem(
                id=item_id,
                title=str(raw.get("title") or base.title).strip(),
                status=status,
                evidence=raw.get("evidence", base.evidence),
            )
        result = [merged.get(item.id, item) for item in self.items]
        return result, failures

    # ── 应用 ──────────────────────────────────────────────────────
    def apply(self, mode: str, raw_items: list) -> tuple[bool, list[str], dict]:
        """校验并应用一次更新。返回 (是否成功, 失败原因, 结果摘要)。

        任何一项不通过 → 整体不落地（旧计划保持原样），由模型自行修正后重试。
        """
        if mode == MODE_SET:
            parsed, failures = self._validate_set(raw_items)
        elif mode == MODE_UPDATE:
            parsed, failures = self._validate_update(raw_items)
        else:
            return False, [f"未知 mode：{mode}（只允许 set / update）"], {}

        if failures:
            return False, failures, {}

        # 证据核对：只对本次「变成 done」的项做，避免每轮重复校验历史项
        previous = {item.id: item for item in self.items}
        evidence_failures: list[str] = []
        for item in parsed:
            if item.status != STATUS_DONE:
                item.verified = None
                continue
            was_done = previous.get(item.id)
            if was_done is not None and was_done.status == STATUS_DONE and item.evidence is None:
                # 已经是 done 且没给新证据：沿用上次的校验结论
                item.verified = was_done.verified
                item.evidence = was_done.evidence
                continue
            if item.evidence is None:
                evidence_failures.append(
                    f"计划项「{item.title}」标为 done 但没有 evidence："
                    "完成必须能指向本轮真实的工具调用"
                )
                continue
            resolved, reasons = resolve_evidence(item.evidence, self.facts)
            if reasons or resolved is None:
                evidence_failures.extend(
                    f"计划项「{item.title}」证据核验未通过：{reason}" for reason in reasons
                )
            else:
                # 存解析后的证据（含命中的 tool_call_ids）：界面上能点回到底哪次调用
                item.evidence = resolved
                item.verified = True

        if evidence_failures:
            return False, evidence_failures, {}

        self.items = parsed
        self.revision += 1
        self.updated_at = _now()
        return True, [], self.summary()

    def summary(self) -> dict:
        counts = {status: 0 for status in PLAN_STATUSES}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "total": len(self.items),
            "done": counts[STATUS_DONE],
            "in_progress": counts[STATUS_IN_PROGRESS],
            "pending": counts[STATUS_PENDING],
            "blocked": counts[STATUS_BLOCKED],
            "revision": self.revision,
        }

    def snapshot(self) -> dict:
        return {
            "request_id": self.request_id,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "items": [item.to_dict() for item in self.items],
        }

    def unverified_done(self) -> list[str]:
        """标了 done 却没通过校验的项（理论上进不来；留作收尾断言）。"""
        return [item.id for item in self.items if item.status == STATUS_DONE and not item.verified]


def to_snapshot(plan: RunPlan | None):
    """把内存态转成事件/历史用的快照模型；没有计划项时返回 None（字段整体省略）。"""
    from ..agentstream.types import PlanItemSnapshot, PlanSnapshot

    if plan is None or not plan.items:
        return None
    return PlanSnapshot(
        request_id=plan.request_id,
        revision=plan.revision,
        updated_at=plan.updated_at,
        items=[PlanItemSnapshot(**item.to_dict()) for item in plan.items],
    )


# 计划渲染用的状态记号：刻意用纯文本而不是 emoji——它会被塞进模型上下文，
# 记号越简单，模型越不会把它当成需要复述的排版。
_STATUS_MARK = {
    STATUS_PENDING: "[ ]",
    STATUS_IN_PROGRESS: "[~]",
    STATUS_DONE: "[x]",
    STATUS_BLOCKED: "[!]",
}


def render_plan_block(
    items: list,
    *,
    header: str = "【当前计划】",
    note: str = "",
) -> str:
    """把计划渲染成一段可注入上下文的纯文本；没有计划项时返回空串。

    为什么计划必须能被「重放」而不是只活在历史里：
        计划是**当前状态**，历史是**发生过的事**。一旦走了压缩，中段 turn 被折叠
        成摘要，模型就再也看不到自己排到第几步了——它会重新规划、重复已完成的工作，
        或者把「我已经做过」记成「我还没做」。所以计划要像约束一样，从历史之外
        重新注入。

    为什么注入点必须避开 system 开头：
        前缀缓存按 token 块比对，system 开头改一个字，整段缓存归零（实测 4 个字
        能把命中率打到 0）。所以调用方只能把它放在 **system 末尾 / 会话末尾**。
    """
    normalized: list[tuple[str, str, bool]] = []
    for item in items or []:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            status = str(item.get("status") or STATUS_PENDING)
            verified = bool(item.get("verified"))
        else:
            title = str(getattr(item, "title", "") or "").strip()
            status = str(getattr(item, "status", STATUS_PENDING))
            verified = bool(getattr(item, "verified", False))
        if title:
            normalized.append((title, status, verified))
    if not normalized:
        return ""

    counts = {status: 0 for status in PLAN_STATUSES}
    for _, status, _ in normalized:
        counts[status] = counts.get(status, 0) + 1
    summary = " / ".join(
        f"{label} {counts[status]}"
        for status, label in (
            (STATUS_DONE, "已完成"),
            (STATUS_IN_PROGRESS, "进行中"),
            (STATUS_PENDING, "待处理"),
            (STATUS_BLOCKED, "受阻"),
        )
        if counts[status]
    )
    lines = [f"{header}（{summary}）"]
    for title, status, verified in normalized:
        mark = _STATUS_MARK.get(status, "[ ]")
        # 未核实的 done 单独标出来：它和 [x] 看起来一样，但一个是「有调用能证明」，
        # 一个是「模型自己写的」。
        suffix = "（未核实）" if status == STATUS_DONE and not verified else ""
        lines.append(f"{mark} {title}{suffix}")
    if note:
        lines.append(note)
    return "\n".join(lines)


class RunPlanRegistry:
    """活跃 run 的计划注册表（进程内，run 结束即关）。

    为什么按 (user_id, session_id) 索引而不是 request_id：
        工具执行入口只拿得到 user_id / session_id（ToolRegistry.execute 的既有
        签名），拿不到 request_id。而 SessionLanes 保证同一会话同一时刻只有一个
        run，所以这个键在功能上等价于「当前活跃 run」。多副本部署时这份 dict
        不共享——和 workspace_lanes 是同一个边界，届时需要换成 DB 租约。
    """

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], RunPlan] = {}

    def open(
        self,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        facts: list[dict],
        emit: Callable[[Any], None] | None = None,
    ) -> RunPlan:
        key = (user_id, session_id)
        existing = self._plans.get(key)
        if existing is not None and existing.request_id != request_id:
            # 说明上一次 run 的 close 没执行到（异常路径）。计划是「当前态」，
            # 覆盖比拒绝更安全：不能让一条泄漏记录永久挡住这个会话。
            logger.warning(
                "run_plan_leaked_previous key=%s previous_request=%s new_request=%s",
                key,
                existing.request_id,
                request_id,
            )
        plan = RunPlan(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            facts=facts,
            emit=emit,
        )
        self._plans[key] = plan
        return plan

    def current(self, user_id: str, session_id: str) -> RunPlan | None:
        return self._plans.get((user_id, session_id))

    def close(self, request_id: str) -> None:
        for key, plan in list(self._plans.items()):
            if plan.request_id == request_id:
                self._plans.pop(key, None)
