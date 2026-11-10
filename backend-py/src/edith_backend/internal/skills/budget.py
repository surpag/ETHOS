"""Skill 的两层预算（设计稿第十节 / D10）。

为什么预算必须两层
------------------
只限索引：模型拿到"还有 200 个 Skill"的线索后，可以把正文一个个读进上下文——
首轮 prompt 很小，第三轮就爆。只限加载：首轮 system prompt 本身已经超了。
两层各自堵一条路，缺一层等于没做。

本模块只放**纯策略**（怎么截、怎么算），不碰 prompt 渲染、不碰沙箱：
- L1 `plan_index()`：给定每条摘要的字符成本，决定哪些进 system prompt、哪些被省略。
- L2 `SkillLoadBudget`：单 run 内已加载几个 Skill、累计多少字节，以及这次读要不要拒。

L1 为什么是纯函数
----------------
截断必须是**确定性**的：同一份磁盘内容，两次 run 得到的注入文本必须逐字符相同，
否则 system prompt 前缀每次都在变，跨 run 的缓存复用直接归零（实测 94.8% → 0%）。
所以这里不接受任何"按使用频率/最近使用"的输入——那天然是跨 run 变化的。
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field

from .types import SCOPE_PLATFORM, SCOPE_PROJECT, SCOPE_USER

# --------------------------------------------------------------------------
# L1：Discovery 索引预算（进 System Prompt 的摘要）
# --------------------------------------------------------------------------

#: 索引总预算（字符）。单一旋钮：改它一处即可整体缩放。
_INDEX_BUDGET_ENV = "SKILL_INDEX_BUDGET_CHARS"
_DEFAULT_INDEX_BUDGET = 4000

#: 各作用域的份额比例。默认值下正好是 1000 / 1500 / 1500 = 4000（设计稿 10.1）。
#: 用比例而不是绝对值：预算被调大/调小时不必再改三个数，份额自动同比缩放。
_SCOPE_SHARE = {
    SCOPE_PLATFORM: 0.25,
    SCOPE_PROJECT: 0.375,
    SCOPE_USER: 0.375,
}

#: 截断的固定顺序 = 注入的固定顺序。溢出额度按这个顺序补给被截断的层，
#: 于是「谁能用剩余额度」也是确定的（换个顺序，同样的数据会截出不同结果）。
INDEX_ORDER = (SCOPE_PLATFORM, SCOPE_PROJECT, SCOPE_USER)


def index_budget_chars() -> int:
    """索引预算总字符数（环境变量可覆盖；非法值退回默认而不是崩）。"""
    raw = (os.getenv(_INDEX_BUDGET_ENV) or "").strip()
    if not raw:
        return _DEFAULT_INDEX_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_INDEX_BUDGET
    return value if value > 0 else _DEFAULT_INDEX_BUDGET


@dataclass(frozen=True)
class IndexPlan:
    """L1 的裁决结果。

    `kept` 是**原下标**（保持调用方给的顺序）：调用方据此拼文本，本模块不关心
    每行长什么样，也就不会与渲染格式耦合。
    """

    kept: list[int] = field(default_factory=list)
    #: (scope, name)，按截断顺序，供"已省略 N 个"与 trace 使用
    omitted: list[tuple[str, str]] = field(default_factory=list)

    @property
    def omitted_count(self) -> int:
        return len(self.omitted)


def plan_index(costs: list[tuple[str, str, int]]) -> IndexPlan:
    """按份额截断，返回保留项与被省略项。

    `costs` 是 `(scope, name, 字符成本)`，**顺序即注入顺序**（平台 → 项目 → 用户，
    层内按 name 升序）。这里不做排序：排序是调用方的责任，重复排一次会让
    「同一份数据的输出」依赖两处实现对顺序的理解一致。

    两轮分配，解决「一层用不完、另一层被自己的份额挤死」：

    1. **按份额**：每层先拿到自己那份的**下限保障**（防止一个大列表把别人挤没）；
    2. **溢出再分配**：把没用完的份额汇成公共额度，按固定顺序（优先级降序）补给
       被截断的层。没有第二轮的话，用户有 50 个 Skill、平台只有 1 个时，用户层会被
       按 1500 截到 ~15 条，而另外 2500 字符的额度直接浪费——那不是预算，是配额僵化。

    所以份额是**下限**而不是上限：高优先层在第二轮仍可吃掉公共额度。这与
    「平台优先于项目优先于用户」的裁决一致——不应该出现"平台还有内容没列出来，
    而用户层的第 30 条先进了 prompt"这种与优先级相反的分配。

    成本本身超过预算的单项不会被"部分保留"：截断以**条目**为单位，
    半个 Skill 的摘要比没有更糟（模型会以为描述就到那里为止）。
    """
    budget = index_budget_chars()
    # 份额用整数减法而不是浮点累加：浮点会让「刚刚好等于份额」的边界抖动
    shares = {scope: int(budget * ratio) for scope, ratio in _SCOPE_SHARE.items()}

    by_scope: dict[str, list[int]] = {scope: [] for scope in INDEX_ORDER}
    for index, (scope, _name, _cost) in enumerate(costs):
        by_scope.setdefault(scope, []).append(index)

    used = {scope: 0 for scope in INDEX_ORDER}
    kept: list[int] = []
    deferred: dict[str, list[int]] = {scope: [] for scope in INDEX_ORDER}

    for scope in INDEX_ORDER:
        limit = shares.get(scope, 0)
        for index in by_scope.get(scope, ()):
            cost = costs[index][2]
            if used[scope] + cost <= limit:
                used[scope] += cost
                kept.append(index)
            else:
                deferred[scope].append(index)

    # 未知作用域不进 `INDEX_ORDER` 也就不占份额、不会被保留：它们会落进 `omitted`。
    # 这是诚实的降级（"有个东西没进 prompt，且它叫什么"），而不是悄悄塞进某个层。

    pool = sum(max(0, shares[scope] - used[scope]) for scope in INDEX_ORDER)
    for scope in INDEX_ORDER:
        for index in deferred.get(scope, ()):
            cost = costs[index][2]
            if pool >= cost:
                pool -= cost
                kept.append(index)
            else:
                break  # 该层剩余项按顺序全丢，不再跳着捡（跳着捡会让"被省略"不可解释）

    omitted: list[tuple[str, str]] = []
    kept_set = set(kept)
    for index, (scope, name, _cost) in enumerate(costs):
        if index not in kept_set:
            omitted.append((scope, name))

    kept.sort()  # 恢复调用方给的顺序（注入顺序 = 优先级，不能因为两轮分配被打乱）
    return IndexPlan(kept=kept, omitted=omitted)


# --------------------------------------------------------------------------
# L2：Skill 加载预算（单 run 内实际读的正文）
# --------------------------------------------------------------------------

#: 单 run 最多加载几个**不同的** Skill
MAX_SKILLS_PER_RUN = 3
#: 单 run 累计返回的正文字节上限（60KB）
MAX_SKILL_BYTES_PER_RUN = 60 * 1024
#: 预警线比例（60KB × 0.8 = 48KB）：到这条线开始提醒剩余额度，但仍然放行
WARN_RATIO = 0.8


@dataclass
class SkillLoadBudget:
    """单 run 的 Skill 加载账本。

    两个指标必须分开记，理由见设计稿 10.2：
    - **数量**按「不同的 Skill 数」计——否则分页读完一个 100KB 的 SKILL.md 会被
      当成 5 个 Skill 而误杀；
    - **体积**按「累计返回字节」计——否则反复读同一段就能绕过上下文预算。

    计量口径是**实际返回给模型的字节**，不是文件大小：`offset`/`limit` 分页读一个
    100KB 文件，累计必须是 100KB，也不能因为只读了片段就按整份文件先记 100KB。
    """

    max_skills: int = MAX_SKILLS_PER_RUN
    max_bytes: int = MAX_SKILL_BYTES_PER_RUN
    #: skill 标识（沙箱内目录，如 `skills/custom/demo`）→ 累计返回字节
    loaded: dict[str, int] = field(default_factory=dict)
    #: 被硬拒绝的次数：用于发现"模型在硬撞配额"
    denied: int = 0

    @property
    def loaded_bytes(self) -> int:
        return sum(self.loaded.values())

    @property
    def loaded_skills(self) -> list[str]:
        return sorted(self.loaded)

    def charge(self, key: str, size: int) -> ChargeDecision:
        """尝试为一读取入账，返回「放行 / 拒绝」及提示。

        拒绝时不改账本：这次读取没有返回任何正文，计入体积会让模型在
        「被拒 → 重试 → 被拒」的循环里把预算越撞越小。
        """
        size = max(0, int(size))
        known = key in self.loaded
        if not known and len(self.loaded) >= self.max_skills:
            self.denied += 1
            return ChargeDecision(
                allowed=False,
                reason="skill_count",
                message=(
                    f"本轮已加载 {len(self.loaded)} 个 Skill（上限 {self.max_skills} 个），"
                    "不能再加载新的 Skill 正文。"
                ),
            )
        if self.loaded_bytes + size > self.max_bytes:
            self.denied += 1
            return ChargeDecision(
                allowed=False,
                reason="skill_bytes",
                message=(
                    f"本轮 Skill 正文累计 {self.loaded_bytes} 字节，"
                    f"再读 {size} 字节会超过上限 {self.max_bytes} 字节。"
                ),
            )

        self.loaded[key] = self.loaded.get(key, 0) + size
        warn_at = int(self.max_bytes * WARN_RATIO)
        warning = ""
        if self.loaded_bytes >= warn_at:
            warning = (
                f"本轮 Skill 正文已累计 {self.loaded_bytes} 字节"
                f"（预警线 {warn_at}，上限 {self.max_bytes}）。"
                "后续请优先读片段或依赖摘要，避免触顶。"
            )
        return ChargeDecision(allowed=True, reason="", message=warning)

    def snapshot(self) -> dict[str, object]:
        """trace 用的快照（键名与设计稿 10.2 对齐）。"""
        return {
            "loadedSkills": self.loaded_skills,
            "loadedBytes": self.loaded_bytes,
            "denied": self.denied,
        }


@dataclass(frozen=True)
class ChargeDecision:
    """一次入账的结论。

    `allowed=False` 时必须转成**结构化 tool error**而不是抛异常：模型据此能自救
    （改读片段、或直接依赖 L1 摘要），抛异常只会让整轮工具调用失败。
    """

    allowed: bool
    reason: str = ""
    message: str = ""


def new_skill_load_budget() -> SkillLoadBudget:
    return SkillLoadBudget()


#: 当前 run 的账本。用 ContextVar 而不是进程级 dict：
#: - 作用域天然等于 run 所在的 task（`asyncio.create_task` 会复制上下文），
#:   并发 run 之间互不可见；
#: - run 结束随 task 消失，不需要 TTL / 回收（进程级 dict 必须自己处理泄漏，
#:   而漏一次就是「上个 run 的额度被下个 run 继承」这种极难复现的 bug）；
#: - 代价：不绑定时不计量（单测直接调工具）。这是刻意的——预算不是安全边界，
#:   它的绑定点只有一处（`agentrun.execution.chat`），由测试钉住。
_CURRENT: ContextVar[SkillLoadBudget | None] = ContextVar(
    "edith_skill_load_budget", default=None
)


def bind_skill_load_budget(budget: SkillLoadBudget) -> None:
    """在 run 入口绑定账本（每次 run 覆盖，天然幂等）。"""
    _CURRENT.set(budget)


def current_skill_load_budget() -> SkillLoadBudget | None:
    return _CURRENT.get()


def reset_skill_load_budget() -> None:
    """仅测试使用：把当前上下文的账本清空。"""
    _CURRENT.set(None)


def charge_skill_load(key: str, size: int) -> ChargeDecision | None:
    """按当前 run 的账本入账；没有绑定账本时返回 None（不计量）。"""
    budget = _CURRENT.get()
    if budget is None:
        return None
    return budget.charge(key, size)


__all__ = [
    "INDEX_ORDER",
    "MAX_SKILLS_PER_RUN",
    "MAX_SKILL_BYTES_PER_RUN",
    "WARN_RATIO",
    "ChargeDecision",
    "IndexPlan",
    "SkillLoadBudget",
    "bind_skill_load_budget",
    "charge_skill_load",
    "current_skill_load_budget",
    "index_budget_chars",
    "new_skill_load_budget",
    "plan_index",
    "reset_skill_load_budget",
]
