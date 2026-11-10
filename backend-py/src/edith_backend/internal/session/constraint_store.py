"""用户约束的持久化与渲染。

存在的理由（这是与「摘要」的本质区别）：
    指令性语句（"必须用 Python""不要动 migrations/"）只要进入压缩流水线，
    就会被改写成陈述句（"讨论了语言选型"）——**改写即失效**，而且是按代际
    不可逆地衰减。所以约束不能住在对话历史里，必须住在历史之外：
      · 原文存储，不做任何摘要或改写；
      · 每次 run 开始重新注入 system prompt，不参与压缩；
      · 生命周期独立（superseded / revoked），不随消息被折叠。

几条设计约束（都是刻意的）：
    1. **原文不改写**。存用户原话。这是它在 N 代压缩后仍然可用的根本原因。
    2. **去重在数据库层**。部分唯一索引保证同一作用域下不会有两条相同的
       active 约束——应用层的 if 判断可以被并发绕过，索引不能。
    3. **不静默丢弃**。注入时有字符预算，超了按 priority 截断，但必须把
       「省略了几条」写回给调用方（用于 trace 与 UI 提示），绝不悄悄消失。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import aiosqlite

# 作用域：会话级（普通对话）/ 工作区级（项目约定，跨会话复用）/ 用户级（偏好）
SCOPE_SESSION = "session"
SCOPE_WORKSPACE = "workspace"
SCOPE_USER = "user"

KIND_HARD = "hard"
KIND_SOFT = "soft"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED = "revoked"

# 注入段标题。措辞刻意用「必须遵守」而不是「参考」——模型对前者的遵从率明显更高。
_BLOCK_TITLE = "【硬约束（必须遵守）】"

# 强祈使词 → hard：这些是用户明确划定边界的表达，违反代价高
_HARD_MARKERS = (
    "必须",
    "禁止",
    "不要",
    "不准",
    "不得",
    "务必",
    "绝不能",
    "严禁",
    "一定要",
    "只能",
)
# 弱祈使词 → soft：偏好类，允许后续被推翻
_SOFT_MARKERS = ("记住", "记得", "尽量", "最好", "以后", "默认")

# ── 一次性 vs 持久：约束表只收「持久」的那一类 ────────────────────────
# 误收的代价比漏收大：一条已经失效的一次性指令会被每轮重新注入，模型会一直
# 遵守一个用户早就忘了的要求，而用户看不到它在生效——排查成本极高。
# 判据不是「这句话听起来像不像命令」，而是「它约束的对象有没有跨轮寿命」：
#
#   a) 明确的一次性限定 → 不收（"这次先不要动 tests/"、"今天只用 CPU"）
#   b) 约束对象是**本轮 Agent 行为**而非项目状态 → 不收
#      （"不要调用任何工具"、"只回复 ok"、"不要输出解释"）
#   c) 剩余（必须/禁止 + 具体对象）→ 收，因为对象是产物或项目约定
#
# 漏收可由用户显式重申补上（重复出现即重新写入）；误收则无人纠正。
_ONE_OFF_MARKERS = ("这次", "本次", "这一轮", "本轮", "今天", "暂时", "接下来")
_AGENT_BEHAVIOR_MARKERS = ("工具", "调用", "回复", "输出", "回答", "解释", "思考")
_PERSISTENT_MARKERS = ("以后", "每次", "一律", "始终", "总是", "永远", "记住", "今后", "后续")

_MAX_CONSTRAINT_CHARS = 200
_MIN_CONSTRAINT_CHARS = 4
_SPLIT_RE = re.compile(r"[。！？\n;；,，]+")

# 注入预算：~1200 字符（中文约 600-800 token）。硬约束超过十条后模型对每一条
# 的遵从率都会下降，「多注入」比「少注入」更危险，所以预算的意义是保护注意力。
_DEFAULT_BUDGET_CHARS = 1200


def constraints_enabled() -> bool:
    """总开关。抽取与注入共用一处判断，避免两边语义漂移。"""
    return os.environ.get("EDITH_CONSTRAINTS_ENABLED", "1") not in (
        "0",
        "false",
        "False",
    )


def constraint_budget_chars() -> int:
    try:
        return int(os.environ.get("EDITH_CONSTRAINT_BUDGET_CHARS", ""))
    except ValueError:
        return _DEFAULT_BUDGET_CHARS


@dataclass(frozen=True)
class Constraint:
    """一条约束（原文，不做任何改写）。"""

    id: int
    scope: str
    scope_id: str
    text: str
    kind: str
    priority: int


@dataclass(frozen=True)
class Extracted:
    """规则抽取的产物（尚未落库）。"""

    text: str
    kind: str


def extract_constraints(message: str) -> list[Extracted]:
    """从一条**用户消息**里抽取祈使句约束。

    只用规则，不调模型，理由有两个：
      · 成本为 0 且确定性可测（模型抽取会把「你今天必须早点睡」也收进来）；
      · 抽取质量差可以改规则，而模型抽取的质量差异往往是不可归因的。

    宁可少收，不可滥收：约束表一旦膨胀，会稀释模型对真正硬约束的注意力，
    这是这类设计最常见的失败方式。
    """
    if not message:
        return []

    found: list[Extracted] = []
    seen: set[str] = set()
    for raw in _SPLIT_RE.split(message):
        sentence = raw.strip()
        if not (_MIN_CONSTRAINT_CHARS <= len(sentence) <= _MAX_CONSTRAINT_CHARS):
            continue
        kind = _classify(sentence)
        if kind is None:
            continue
        # 同一条消息内去重（同一句话重复出现只留一条）
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(Extracted(text=sentence, kind=kind))
    return found


def _classify(sentence: str) -> str | None:
    """判断一句话是不是约束，以及强弱。"""
    if _is_one_off(sentence):
        return None
    for marker in _HARD_MARKERS:
        if marker in sentence:
            return KIND_HARD
    for marker in _SOFT_MARKERS:
        if marker in sentence:
            return KIND_SOFT
    return None


def _is_one_off(sentence: str) -> bool:
    """一次性指令不该进约束表（判据见上方 _ONE_OFF_MARKERS 的说明）。

    有持久副词时一律视为持久——"以后每次调用工具前先读文件"虽然提到工具，
    但它约束的是流程约定，不是本轮动作。
    """
    if any(marker in sentence for marker in _PERSISTENT_MARKERS):
        return False
    if any(marker in sentence for marker in _ONE_OFF_MARKERS):
        return True
    # "先" 单字太容易误伤（"先读文件再改"），只在句首出现时才算限定词
    if sentence.startswith("先"):
        return True
    return any(marker in sentence for marker in _AGENT_BEHAVIOR_MARKERS)


def _priority(kind: str) -> int:
    """截断时保留谁：hard 永远先于 soft。"""
    return 200 if kind == KIND_HARD else 100


class ConstraintStore:
    """约束表的访问器。与 SessionStore 共用同一个连接（同一份数据库）。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------ 写入

    async def add(
        self,
        user_id: str,
        *,
        scope: str,
        scope_id: str,
        text: str,
        kind: str = KIND_SOFT,
        source: str = "user_explicit",
        source_message_id: int | None = None,
    ) -> int | None:
        """写入一条约束；已存在同文 active 约束时返回 None（幂等）。"""
        text = text.strip()
        if not text:
            return None
        try:
            cursor = await self._db.execute(
                """
                INSERT INTO constraints
                    (user_id, scope, scope_id, text, kind, source,
                     source_message_id, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    scope,
                    scope_id,
                    text,
                    kind,
                    source,
                    source_message_id,
                    _priority(kind),
                ),
            )
        except aiosqlite.IntegrityError:
            # 部分唯一索引挡下了重复：这是正常路径，不是错误
            return None
        await self._db.commit()
        return int(cursor.lastrowid or 0)

    async def capture_message(
        self,
        user_id: str,
        *,
        session_id: str,
        workspace_id: str | None,
        message: str,
        source_message_id: int | None = None,
    ) -> list[int]:
        """从用户消息里抽取并落库，返回新写入的约束 id。

        作用域选择：有工作区就挂工作区（项目约定应当跨会话复用），
        否则挂当前会话。不双写——同一句话存两份，后续 supersede 会劈叉。
        """
        items = extract_constraints(message)
        if not items:
            return []

        if workspace_id:
            scope, scope_id = SCOPE_WORKSPACE, workspace_id
        else:
            scope, scope_id = SCOPE_SESSION, session_id

        new_ids: list[int] = []
        for item in items:
            new_id = await self.add(
                user_id,
                scope=scope,
                scope_id=scope_id,
                text=item.text,
                kind=item.kind,
                source="user_explicit",
                source_message_id=source_message_id,
            )
            if new_id:
                new_ids.append(new_id)
        return new_ids

    async def link_source(
        self, user_id: str, constraint_ids: list[int], message_id: int
    ) -> None:
        """回填溯源消息 id。

        抽取发生在消息落库之前（约束必须在本轮配置加载前就已存在），所以
        溯源 id 只能事后补。没有它，「这条约束是谁在什么时候说的」就查不回去，
        后面所有审计与 supersede 判定都失去了依据。
        """
        if not constraint_ids or not message_id:
            return
        placeholders = ",".join("?" for _ in constraint_ids)
        await self._db.execute(
            f"""
            UPDATE constraints SET source_message_id = ?
             WHERE user_id = ? AND id IN ({placeholders})
            """,
            (message_id, user_id, *constraint_ids),
        )
        await self._db.commit()

    async def revoke(self, user_id: str, constraint_id: int) -> bool:
        """撤销一条约束。不物理删除——保留下来才能审计「为什么当时那么做」。"""
        cursor = await self._db.execute(
            """
            UPDATE constraints SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ? AND status = ?
            """,
            (STATUS_REVOKED, constraint_id, user_id, STATUS_ACTIVE),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def supersede(self, user_id: str, old_id: int, new_id: int) -> bool:
        """用新约束取代旧约束：旧的不删，标记失效并指向继任者。"""
        cursor = await self._db.execute(
            """
            UPDATE constraints
               SET status = ?, superseded_by = ?, updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND user_id = ? AND status = ?
            """,
            (STATUS_SUPERSEDED, new_id, old_id, user_id, STATUS_ACTIVE),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ 读取

    async def list_active(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Constraint]:
        """列出当前 run 适用的全部 active 约束（工作区 + 会话 + 用户级）。"""
        clauses = ["user_id = ?", "status = ?"]
        params: list[object] = [user_id, STATUS_ACTIVE]
        scoped: list[str] = []
        if workspace_id:
            scoped.append("(scope = ? AND scope_id = ?)")
            params.extend([SCOPE_WORKSPACE, workspace_id])
        if session_id:
            scoped.append("(scope = ? AND scope_id = ?)")
            params.extend([SCOPE_SESSION, session_id])
        scoped.append("scope = ?")
        params.append(SCOPE_USER)

        cursor = await self._db.execute(
            f"""
            SELECT id, scope, scope_id, text, kind, priority
              FROM constraints
             WHERE {' AND '.join(clauses)} AND ({' OR '.join(scoped)})
             ORDER BY priority DESC, id ASC
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            Constraint(
                id=int(row[0]),
                scope=str(row[1]),
                scope_id=str(row[2]),
                text=str(row[3]),
                kind=str(row[4]),
                priority=int(row[5]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 渲染

    async def render_for_prompt(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
        budget_chars: int = 1200,
    ) -> tuple[str, int, int]:
        """渲染可直接追加到 system prompt 的约束段。

        返回 (文本, 注入条数, 省略条数)。省略条数必须由调用方上报，
        否则「约束没生效」会变成一件无法归因的事。
        """
        items = await self.list_active(
            user_id, session_id=session_id, workspace_id=workspace_id
        )
        if not items:
            return "", 0, 0

        block, injected = render_constraints(items, budget_chars=budget_chars)
        return block, injected, len(items) - injected


def render_constraints(
    items: list[Constraint], *, budget_chars: int = 1200
) -> tuple[str, int]:
    """把约束列表渲染成紧凑的 prompt 段落，超出预算时按 priority 截断。

    预算的意义不是省钱，而是**保护注意力**：硬约束超过十条以后，模型对每一条
    的遵从率都会下降，此时「多注入」比「少注入」更危险。所以宁可截断并明说。
    """
    if not items:
        return "", 0

    lines: list[str] = []
    used = len(_BLOCK_TITLE) + 2
    injected = 0
    for item in items:
        tag = "必须" if item.kind == KIND_HARD else "偏好"
        line = f"- [{tag}] {item.text}"
        cost = len(line) + 1
        if used + cost > budget_chars:
            break
        lines.append(line)
        used += cost
        injected += 1

    omitted = len(items) - injected
    if omitted:
        lines.append(f"（另有 {omitted} 条约束因长度限制未展示，如需请显式询问用户）")

    return _BLOCK_TITLE + "\n" + "\n".join(lines), injected
