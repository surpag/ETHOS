"""Skill 作用域解析：把「平台 + 用户 + 项目」收敛成**唯一**的解析产物。

为什么要这一层
--------------
注入侧的旧实现是「两段拼接」：`configuration.py` 先取平台摘要，再取用户索引原文，
然后由 `skill_instruction()` 拼成两段带固定小标题的文本。这让「有哪些 Skill 生效」
这件事**没有单一落点**——两个来源各自读取、各自格式化，谁生效只能靠读拼接结果推断。

引入项目级后问题变成三种来源 × 覆盖关系，靠拼接顺序表达优先级是不可验证的
（顺序错了没人能测出来）。所以解析收敛成一个产物 `ResolvedCatalog`：
`entries` 里出现的 name 就是「已胜出的」（**name 唯一**），`shadows`/`conflicts`
只走状态侧信道。优先级因此是**数据结构性质**，可以直接 assert。

裁决规则（设计稿 4.2 / 5.1，D1 / D2 / D8）
-----------------------------------------
| 情形 | 结果 |
| --- | --- |
| name 只在一层出现（C1–C3） | 注入该条 |
| 命中平台保留名（C4 / C5 / C7） | 该 name 的**全部**自定义版本都不注入 → `conflicts` |
| 用户级 + 项目级同名（C6） | 项目级胜，用户级进 `shadows` |
| 同层内两个目录解析出同一 name（R4） | 按**目录名**升序取第一个，其余进 `conflicts`，该层 `partial` |
| 某层 `unavailable`（R5） | 只注入读得到的层（fail-open），**不猜**抵消关系，状态原样上报 |

R5 之所以是 fail-open：裁决需要两层的完整名单。项目层读不到时「哪些 U 被 J 覆盖」
是未知的，无法做减法；此时唯一诚实且可执行的规则是「给出读得到的 + 把未知明确上报」。
fail-closed 会让一次存储抖动抹掉用户所有 Skill，代价远大于"可能用了旧版本"。

两个不变量（必须守住）
--------------------
1. **只读不写**：不缓存、不落库、不改权威副本。`shadows` 是每次重算的派生结果，
   不是持久化的覆盖关系——这正是 D3「删项目级即自动恢复用户级」的实现方式。
2. **`shadows`/`conflicts` 绝不是删除依据**：它们只描述「这一次怎么裁」，
   任何代码都不得因为某个 name 出现在这里就删除、移动或改写磁盘上的文件。

B2-3 起注入完全由 `entries` 渲染
-------------------------------
B0–B2-2 期间注入走的是 `user_overview` 索引原文（为了逐字节不变的约束），于是
「项目级覆盖用户级」「保留名冲突」两种情形下，注入文本与 `entries` 会短暂不一致：
文本里还列着被压住的那条。B2-3 把渲染来源切成 `entries` 并删掉该字段，这类
不一致从此**结构上不可表达**——注入文本与裁决结果同源，不可能各说一套。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from .catalog import Catalog, parse_user_overview
from .storage.base import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_PARTIAL,
    SkillIndex,
)
from .types import (
    CUSTOM_PATH,
    IMPLEMENTED_SCOPES,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    SCOPE_USER,
    SYSTEM_PATH,
    SkillSummary,
)

if TYPE_CHECKING:  # 运行期按需导入：storage.project → storage.local → catalog 会成环
    from .storage import SkillScopeStores

logger = logging.getLogger(__name__)

#: 裁决原因：三条都是稳定字符串（进 trace / HTTP，不要随手改文案）
RESERVED_REASON = "platform reserved name"
SHADOW_REASON = "project overrides user"
DUPLICATE_REASON = "duplicate name within scope"


@dataclass(frozen=True)
class ResolvedEntry:
    """一个参与注入的 Skill。B2-2 起 name 在 catalog 内唯一。"""

    name: str
    description: str
    scope: str
    #: 相对沙箱工作区根的目录，如 `skills/custom/{dir}`。模型据此读 SKILL.md。
    sandbox_path: str
    #: 权威副本里的真实目录名（R4 的 tie-break 依据，不参与注入）
    source_dir: str = ""


@dataclass(frozen=True)
class ScopeReport:
    """某个作用域本次解析的状态——「没有 Skill」与「读不到」必须可区分。"""

    scope: str
    status: str = STATUS_EMPTY
    reason: str = ""
    healed: bool = False
    #: 该层**解析出的**条数（裁决前）。别拿它当"注入了几条"：保留名/覆盖会减少注入量。
    count: int = 0


@dataclass(frozen=True)
class ScopeDecision:
    """覆盖关系的一条记录。

    `shadows`（正常覆盖）与 `conflicts`（无法裁决）共用这个结构，区别只在语义：
    前者是规则内的预期行为，后者需要人介入。

    - C6：`winner_scope=project` / `loser_scope=user`
    - C4/C5/C7：`winner_scope=platform` / `loser_scope=user|project` —— C7 会产生
      **两条**记录（平台同时淘汰两层自定义版本），刻意不在一条里塞两个 loser：
      一条记录 = 一次裁决，否则消费方还得再解析字段。
    - R4：`winner_scope == loser_scope`，`detail` 里写明保留了哪个目录。

    **它只描述「本次解析怎么裁的」，绝不是删除依据。** 任何实现都不得因为某个
    name 出现在这里就删除、移动或改写权威副本里的文件（见设计稿 D1/D3）。
    """

    name: str
    winner_scope: str
    loser_scope: str
    reason: str = ""
    #: 补充事实（保留了哪个目录），只用于诊断展示
    detail: str = ""


@dataclass(frozen=True)
class ResolvedCatalog:
    """一次解析的完整结果，注入侧唯一的输入。

    `reports` 按作用域存状态。B2-3 起不再携带索引原文（`user_overview` 已删除）：
    注入统一从 `entries` 渲染，于是「压住的那条还列在文本里」这类不一致在结构上
    不再可能。
    """

    entries: list[ResolvedEntry] = field(default_factory=list)
    reports: dict[str, ScopeReport] = field(default_factory=dict)
    #: 项目级覆盖用户级（D1）
    shadows: list[ScopeDecision] = field(default_factory=list)
    #: 平台保留名冲突（D8）与同层重名（R4）
    conflicts: list[ScopeDecision] = field(default_factory=list)

    def by_scope(self, scope: str) -> list[ResolvedEntry]:
        return [e for e in self.entries if e.scope == scope]

    def report(self, scope: str) -> ScopeReport:
        return self.reports.get(scope) or ScopeReport(scope=scope)


class SkillResolver:
    """把一次 run 的 Skill 集合解析成 `ResolvedCatalog`。

    依赖 `Catalog`（平台摘要 + 用户索引）与 `SkillScopeStores`（项目级索引）。
    不感知后端是 local 还是 e2b；`scopes` 缺失时项目层按「未配置」上报。
    """

    def __init__(
        self, catalog: Catalog, scopes: SkillScopeStores | None = None
    ) -> None:
        self._catalog = catalog
        self._scopes = scopes

    async def resolve(
        self, user_id: str, workspace_id: str | None = None
    ) -> ResolvedCatalog:
        """解析三层并裁决。只读不写，每次 run 重算（D3 依赖这条性质）。"""
        platform = self._platform_entries()
        user_index = await self._catalog.read_index(user_id)
        user = _entries_from(user_index, SCOPE_USER, CUSTOM_PATH)

        project_index, project_base = await self._read_project(user_id, workspace_id)
        project = _entries_from(project_index, SCOPE_PROJECT, project_base)

        reports = {
            SCOPE_PLATFORM: ScopeReport(
                scope=SCOPE_PLATFORM,
                status=STATUS_OK if platform else STATUS_EMPTY,
                count=len(platform),
            ),
            SCOPE_USER: ScopeReport(
                scope=SCOPE_USER,
                status=user_index.status,
                reason=user_index.reason,
                healed=user_index.healed,
                count=len(user),
            ),
            SCOPE_PROJECT: ScopeReport(
                scope=SCOPE_PROJECT,
                status=project_index.status,
                reason=project_index.reason,
                healed=project_index.healed,
                count=len(project),
            ),
        }

        entries, shadows, conflicts = _adjudicate(platform, user, project, reports)
        return ResolvedCatalog(
            entries=entries,
            reports=reports,
            shadows=shadows,
            conflicts=conflicts,
        )

    def _platform_entries(self) -> list[ResolvedEntry]:
        return [
            ResolvedEntry(
                name=summary.name,
                description=summary.description,
                scope=SCOPE_PLATFORM,
                sandbox_path=f"{SYSTEM_PATH}/{summary.dir_name or summary.name}",
                source_dir=summary.dir_name or summary.name,
            )
            for summary in self._catalog.list_system_summaries()
        ]

    async def _read_project(
        self, user_id: str, workspace_id: str | None
    ) -> tuple[SkillIndex, str]:
        """读项目层；不成立时返回空索引 + 空根路径（R0）。

        三种「不成立」都不是错误，因此不抛异常、也不把整个解析搞失败：
        - 没有工作区上下文（纯聊天会话，R0）；
        - 项目级尚未纳入已实现作用域；
        - 没有注入作用域存储（精简部署）。

        注意**不可用的后端（e2b）不在这里判断**：它走 `read_index` 返回
        `unavailable` + 原因，由 R5 按「未知」处理。这里若把它当"没有"，
        失败就从状态字段里消失了。
        """
        key = (workspace_id or "").strip()
        if not key or SCOPE_PROJECT not in IMPLEMENTED_SCOPES:
            return SkillIndex(status=STATUS_EMPTY), ""
        if self._scopes is None:
            return (
                SkillIndex(
                    status=STATUS_EMPTY, reason="project skill storage not configured"
                ),
                "",
            )
        # 路径与 push 的写入目录必须同源，否则索引指向一个不存在的目录
        from .storage.project import project_index_base

        index = await self._scopes.project(key).read_index(user_id)
        return index, project_index_base(key)


def _entries_from(
    index: SkillIndex, scope: str, base_path: str
) -> list[ResolvedEntry]:
    """把一层索引变成条目。

    结构化摘要缺失时退回解析索引原文（e2b 卷内只有 markdown）。这条路拿不到
    目录名，`dir_name` 留空 → 路径退回 frontmatter 的 name（与旧行为一致）。
    """
    if not base_path:
        return []
    summaries: list[SkillSummary] = index.summaries or parse_user_overview(index.text)
    return [
        ResolvedEntry(
            name=summary.name,
            description=summary.description,
            scope=scope,
            sandbox_path=f"{base_path}/{summary.dir_name or summary.name}",
            source_dir=summary.dir_name or summary.name,
        )
        for summary in summaries
    ]


def _adjudicate(
    platform: list[ResolvedEntry],
    user: list[ResolvedEntry],
    project: list[ResolvedEntry],
    reports: dict[str, ScopeReport],
) -> tuple[list[ResolvedEntry], list[ScopeDecision], list[ScopeDecision]]:
    """R1–R4 的完整裁决（7 种组合全覆盖，无"其他情况"分支）。

    顺序不可交换：**先**同层去重（R4，否则同一层的两条会各自去和另一层比），
    **再**淘汰保留名（R2，平台优先于一切自定义），**最后**才做 J 覆盖 U（R3）。
    R2 先于 R3 的意义：C7 里项目级不该因为"它比用户级更具体"而保留下来。
    """
    conflicts: list[ScopeDecision] = []
    shadows: list[ScopeDecision] = []

    user = _dedupe(user, SCOPE_USER, reports, conflicts)
    project = _dedupe(project, SCOPE_PROJECT, reports, conflicts)

    reserved = {entry.name for entry in platform}
    user = _drop_reserved(user, SCOPE_USER, reserved, conflicts)
    project = _drop_reserved(project, SCOPE_PROJECT, reserved, conflicts)

    # R5 的落地方式：不可用的层在这里天然是空列表，于是「只注入读得到的层」
    # 不需要额外分支，也不会凭空产生 shadow 记录（未知 ≠ 未覆盖）。
    by_name = {entry.name: entry for entry in project}
    surviving_user: list[ResolvedEntry] = []
    for entry in user:
        winner = by_name.get(entry.name)
        if winner is None:
            surviving_user.append(entry)
            continue
        shadows.append(
            ScopeDecision(
                name=entry.name,
                winner_scope=SCOPE_PROJECT,
                loser_scope=SCOPE_USER,
                reason=SHADOW_REASON,
                detail=f"kept {SCOPE_PROJECT} dir {winner.source_dir}",
            )
        )

    # 顺序 = 优先级降序（平台 → 项目 → 用户），层内按 name 升序：
    # 与遍历顺序、字典插入顺序、并发都无关（5.1 的「确定性」要求）。
    entries = [
        *platform,
        *sorted(project, key=lambda e: e.name),
        *sorted(surviving_user, key=lambda e: e.name),
    ]
    return entries, shadows, conflicts


def _dedupe(
    entries: list[ResolvedEntry],
    scope: str,
    reports: dict[str, ScopeReport],
    conflicts: list[ScopeDecision],
) -> list[ResolvedEntry]:
    """R4：同层两个目录解析出同一 name。

    tie-break 用**目录名升序**而不是 `iterdir` 顺序或 frontmatter 顺序：
    目录名是磁盘上的事实，与实现细节无关；不写死这条规则，将来换扫描实现就会漂。
    """
    ordered = sorted(entries, key=lambda entry: entry.source_dir or entry.name)
    kept: dict[str, ResolvedEntry] = {}
    dropped: list[str] = []
    for entry in ordered:
        winner = kept.get(entry.name)
        if winner is None:
            kept[entry.name] = entry
            continue
        dropped.append(entry.name)
        conflicts.append(
            ScopeDecision(
                name=entry.name,
                winner_scope=scope,
                loser_scope=scope,
                reason=DUPLICATE_REASON,
                detail=(
                    f"kept dir {winner.source_dir}, dropped dir {entry.source_dir}"
                ),
            )
        )
    if dropped and scope in reports:
        report = reports[scope]
        # R6：失败不放大成整层不可用，但该层状态必须变成 partial——否则
        # 「复制目录忘了改名」这条只有在 conflicts 里能找到，而 status 还说 ok。
        reports[scope] = replace(
            report,
            status=STATUS_PARTIAL,
            reason="; ".join(
                part
                for part in (
                    report.reason,
                    "duplicate names: " + ", ".join(sorted(set(dropped))),
                )
                if part
            ),
        )
    return sorted(kept.values(), key=lambda entry: entry.name)


def _drop_reserved(
    entries: list[ResolvedEntry],
    scope: str,
    reserved: set[str],
    conflicts: list[ScopeDecision],
) -> list[ResolvedEntry]:
    """R2：平台保留名淘汰该层的同名自定义版本（不与 J/U 谁优先纠缠）。"""
    kept: list[ResolvedEntry] = []
    for entry in entries:
        if entry.name in reserved:
            conflicts.append(
                ScopeDecision(
                    name=entry.name,
                    winner_scope=SCOPE_PLATFORM,
                    loser_scope=scope,
                    reason=RESERVED_REASON,
                    detail=f"platform dir {entry.name}",
                )
            )
        else:
            kept.append(entry)
    return kept


__all__ = [
    "DUPLICATE_REASON",
    "RESERVED_REASON",
    "SCOPE_PLATFORM",
    "SCOPE_PROJECT",
    "SCOPE_USER",
    "SHADOW_REASON",
    "ResolvedCatalog",
    "ResolvedEntry",
    "ScopeDecision",
    "ScopeReport",
    "SkillResolver",
]
