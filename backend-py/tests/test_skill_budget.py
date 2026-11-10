"""两层预算（D10）的验收：L1 索引截断与 L2 加载额度。

为什么单独一个文件
------------------
预算的判定是**纯策略**，但它的错误形态都是"静默"的：
- L1 截断错 → 注入文本每次 run 都不一样 → 前缀缓存归零（不会报错，只会变慢变贵）；
- L2 计数错 → 要么分页读被误杀（模型读不完一个长 SKILL.md），要么反复读绕过额度
  （上下文悄悄爆掉）。
所以两类都要**逐条钉住数值与文案**，而不是只断言"最后被拒了"。

本文件不装配沙箱：L1 是纯函数，L2 只依赖 `charge_skill_load` 与当前 run 的账本。
`skill_body_key`（哪些路径算"正文"）单独测，因为它是"误算"的唯一入口。
"""

from __future__ import annotations

import pytest

from edith_backend.internal.sandbox.tools import SandboxToolSet, skill_body_key
from edith_backend.internal.skills.budget import (
    MAX_SKILL_BYTES_PER_RUN,
    MAX_SKILLS_PER_RUN,
    SkillLoadBudget,
    bind_skill_load_budget,
    charge_skill_load,
    index_budget_chars,
    plan_index,
    reset_skill_load_budget,
)
from edith_backend.internal.skills.types import (
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    SCOPE_USER,
)


@pytest.fixture(autouse=True)
def _clean_budget():
    """账本挂在 ContextVar 上，测试之间必须显式清空（否则后一个测试继承前一个的额度）。"""
    reset_skill_load_budget()
    yield
    reset_skill_load_budget()


# --------------------------------------------------------------------------
# L1：索引预算
# --------------------------------------------------------------------------
def _costs(spec: list[tuple[str, str, int]]) -> list[tuple[str, str, int]]:
    return spec


def test_index_budget_defaults_and_env(monkeypatch) -> None:
    monkeypatch.delenv("SKILL_INDEX_BUDGET_CHARS", raising=False)
    assert index_budget_chars() == 4000
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "800")
    assert index_budget_chars() == 800
    # 非法值退回默认而不是崩：这是配置项，不该让一次手抖打错导致整个 run 失败
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "not-a-number")
    assert index_budget_chars() == 4000
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "0")
    assert index_budget_chars() == 4000


def test_index_under_budget_keeps_everything(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    costs = [(SCOPE_PLATFORM, "a", 100), (SCOPE_USER, "b", 100)]
    plan = plan_index(costs)
    assert plan.kept == [0, 1]
    assert plan.omitted == []
    assert plan.omitted_count == 0


def test_index_truncation_is_deterministic(monkeypatch) -> None:
    """同一份输入两次调用必须逐项相同——L1 的不确定性会直接打掉前缀缓存。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    costs = [(SCOPE_USER, f"skill-{i:02d}", 300) for i in range(30)]
    first = plan_index(costs)
    second = plan_index(costs)
    assert first.kept == second.kept
    assert first.omitted == second.omitted
    # 份额是**下限**不是上限：用户层先拿 1500（5 条），公共额度 2500 再补 8 条
    assert len(first.kept) == 13
    assert first.omitted_count == 17
    # 保留的是**前缀**，不是随机挑选（跳着捡会让"被省略"无法解释）
    assert first.kept == list(range(13))


def test_index_share_is_a_floor_for_low_priority_layers(monkeypatch) -> None:
    """平台层超大时不能把用户层挤没：份额先给每层保一块底。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    costs = [(SCOPE_PLATFORM, f"p{i:02d}", 100) for i in range(60)]
    costs += [(SCOPE_USER, "u-one", 100)]
    plan = plan_index(costs)

    kept_scopes = {costs[i][0] for i in plan.kept}
    assert SCOPE_USER in kept_scopes, "用户层的一条必须在（下限保障）"
    # 总预算 4000 / 100 = 40 条；平台拿剩下的 39 条（第二轮吃掉公共额度）
    assert len(plan.kept) == 40
    assert sum(1 for i in plan.kept if costs[i][0] == SCOPE_PLATFORM) == 39
    assert plan.omitted_count == 21


def test_index_spillover_feeds_truncated_layer(monkeypatch) -> None:
    """某层用不完的份额要给被截断的层，而不是直接浪费。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    # 平台 1 条（只用 100 / 1000），用户层 45 条 × 100 = 4500，总量超预算
    costs = [(SCOPE_PLATFORM, "only", 100)]
    costs += [(SCOPE_USER, f"u{i:02d}", 100) for i in range(45)]
    plan = plan_index(costs)

    user_kept = sum(1 for i in plan.kept if costs[i][0] == SCOPE_USER)
    # 用户份额 1500 → 15 条；公共额度（平台剩 900 + 项目 1500）再补 24 条 = 39
    assert user_kept == 39
    assert plan.omitted_count == 6


def test_index_never_keeps_partial_item(monkeypatch) -> None:
    """单条成本超过整个预算时整条省略，不截半条描述。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "400")
    costs = [(SCOPE_USER, "huge", 500)]
    plan = plan_index(costs)
    assert plan.kept == []
    assert plan.omitted == [(SCOPE_USER, "huge")]
    # 放得下的小条目仍然保留：省略的是那一整条，不是整层
    plan2 = plan_index([(SCOPE_USER, "huge", 500), (SCOPE_USER, "small", 10)])
    assert plan2.kept == [1]
    assert plan2.omitted == [(SCOPE_USER, "huge")]


def test_index_project_layer_is_ordered_between(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    costs = [
        (SCOPE_PLATFORM, "p", 100),
        (SCOPE_PROJECT, "j", 100),
        (SCOPE_USER, "u", 100),
    ]
    plan = plan_index(costs)
    # kept 是原下标升序 = 平台 → 项目 → 用户（注入顺序就是优先级顺序）
    assert plan.kept == [0, 1, 2]


# --------------------------------------------------------------------------
# L2：加载预算
# --------------------------------------------------------------------------
def test_count_three_skills_then_deny_fourth() -> None:
    budget = SkillLoadBudget()
    for i in range(MAX_SKILLS_PER_RUN):
        decision = budget.charge(f"skills/custom/s{i}", 10)
        assert decision.allowed, f"第 {i + 1} 个应当放行"
    denied = budget.charge("skills/custom/s3", 10)
    assert not denied.allowed
    assert denied.reason == "skill_count"
    assert budget.denied == 1
    assert len(budget.loaded) == MAX_SKILLS_PER_RUN


def test_denied_read_does_not_consume_quota() -> None:
    """被拒的读取不计入体积：否则「撞额度 → 重试 → 撞得更狠」会越撞越小。"""
    budget = SkillLoadBudget(max_skills=1)
    budget.charge("skills/custom/a", 1000)
    before = budget.loaded_bytes
    denied = budget.charge("skills/custom/b", 10_000)
    assert not denied.allowed
    assert budget.loaded_bytes == before
    assert set(budget.loaded) == {"skills/custom/a"}


def test_paging_same_skill_counts_one_but_sums_bytes() -> None:
    """分页读完一个 Skill：数量算 1，体积按实际累加（设计稿 10.2 的核心口径）。"""
    budget = SkillLoadBudget()
    key = "skills/custom/长文档"
    for _ in range(5):
        assert budget.charge(key, 10_000).allowed
    assert len(budget.loaded) == 1
    assert budget.loaded_bytes == 50_000
    # 数量还有 2 个空位（只用了 1 个）
    assert budget.charge("skills/custom/other", 10).allowed


def test_bytes_limit_blocks_at_threshold() -> None:
    budget = SkillLoadBudget()
    budget.charge("skills/custom/a", MAX_SKILL_BYTES_PER_RUN - 10)
    assert budget.charge("skills/custom/b", 5).allowed
    denied = budget.charge("skills/custom/c", 10)
    assert not denied.allowed
    assert denied.reason == "skill_bytes"


def test_warn_line_is_advisory_not_blocking() -> None:
    """48KB 只提醒不拦——拦在这里等于把"刚好用完"当成错误。"""
    budget = SkillLoadBudget()
    budget.charge("skills/custom/a", 1)
    decision = budget.charge("skills/custom/a", int(MAX_SKILL_BYTES_PER_RUN * 0.8) - 1)
    assert decision.allowed
    assert decision.message, "越过预警线必须带提示，否则模型不知道自己快到顶了"
    assert "预警线" in decision.message


def test_budget_snapshot_shape() -> None:
    budget = SkillLoadBudget()
    budget.charge("skills/custom/b", 5)
    budget.charge("skills/custom/a", 7)
    budget.denied = 2
    snapshot = budget.snapshot()
    assert snapshot == {
        "loadedSkills": ["skills/custom/a", "skills/custom/b"],
        "loadedBytes": 12,
        "denied": 2,
    }


def test_unbound_budget_does_not_charge() -> None:
    """没有绑定账本时不计量（单测/直调工具）；绑定点只有 run 入口一处。"""
    assert charge_skill_load("skills/custom/a", 100) is None


def test_bound_budget_is_shared_within_run() -> None:
    budget = SkillLoadBudget()
    bind_skill_load_budget(budget)
    assert charge_skill_load("skills/custom/a", 100).allowed
    assert budget.loaded_bytes == 100
    # 再绑一个新账本 = 下一个 run 从零开始（run 之间不累计）
    next_run = SkillLoadBudget()
    bind_skill_load_budget(next_run)
    assert charge_skill_load("skills/custom/a", 100).allowed
    assert next_run.loaded_bytes == 100
    assert budget.loaded_bytes == 100


# --------------------------------------------------------------------------
# 哪些路径算「正文」——误算的唯一入口
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("skills/custom/demo/SKILL.md", "skills/custom/demo"),
        ("skills/system/current-time/SKILL.md", "skills/system/current-time"),
        ("skills/project/ws-1/deploy/SKILL.md", "skills/project/ws-1/deploy"),
        # 非正文：按需片段不计入（刻意的残余风险，见设计稿 10.2）
        ("skills/custom/demo/references/api.md", ""),
        ("skills/custom/demo/scripts/run.py", ""),
        # 工作区文件不是 Skill
        ("workspace/w-1/SKILL.md", ""),
        # 前缀相似的目录不能误判
        ("skills/custom2/demo/SKILL.md", ""),
        # 项目级少一层（workspace_id 或 skill 目录）都不算
        ("skills/project/ws-1/SKILL.md", ""),
        ("skills/project/SKILL.md", ""),
    ],
)
def test_skill_body_key(path: str, expected: str) -> None:
    assert skill_body_key(path) == expected


def test_read_file_denies_fourth_skill_with_structured_error() -> None:
    """工具层：第 4 个不同 Skill 的正文读取返回 command_error（不是抛异常）。"""
    tools = SandboxToolSet(None)  # type: ignore[arg-type] - 只测计量分支，不碰 service
    budget = SkillLoadBudget()
    bind_skill_load_budget(budget)

    for i in range(MAX_SKILLS_PER_RUN):
        assert tools._charge_skill_body(f"skills/custom/s{i}/SKILL.md", "x" * 10) is None

    denied = tools._charge_skill_body("skills/custom/s3/SKILL.md", "x" * 10)
    assert denied is not None
    assert denied["kind"] == "command_error"
    assert denied["retryable"] is False
    assert denied["skill"] == "skills/custom/s3"
    assert denied["suggestion"]


def test_read_file_ignores_non_skill_paths() -> None:
    tools = SandboxToolSet(None)  # type: ignore[arg-type]
    budget = SkillLoadBudget(max_skills=0)
    bind_skill_load_budget(budget)
    # 非 Skill 正文不计量：额度为 0 也照样放行
    assert tools._charge_skill_body("skills/custom/demo/references/a.md", "x" * 100) is None
    assert tools._charge_skill_body("workspace/w-1/main.py", "x" * 100) is None
    assert budget.loaded == {}
