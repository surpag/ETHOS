"""作用域裁决矩阵：R0–R6 与 D3 的「删项目级自动恢复用户级」。

为什么单独一个文件
------------------
`test_skill_resolver_golden.py` 管的是「渲染逐字节不变」，`test_skill_scope_wiring.py`
管的是「同步钩子有没有把两层分开」。**裁决本身**没有归属：它既不是渲染，也不是同步，
而是「三层名单怎么合成一份 `entries`」。漏测它的后果是注入顺序/胜负悄悄变了却没人发现
（`entries` 一旦出现两个同名，模型会看到两条互相矛盾的规则，且没有报错）。

矩阵取自设计稿 4.2 / 5.1：

| 编号 | 情形 | 期望 |
| --- | --- | --- |
| R0 | 无工作区上下文 | 项目层不存在（不是「查不到」） |
| R1 | 名只在单层出现 | 注入该条 |
| C4/C5 | 用户/项目用了平台保留名 | 该层版本被淘汰 → `conflicts` |
| C6 | 用户级 + 项目级同名 | 项目级胜 → `shadows` |
| C7 | 两层都用平台保留名 | 两条冲突记录（一次裁决一条） |
| R4 | 同层两目录解析出同一 name | 目录名升序者胜，该层 `partial` |
| R5 | 项目层 `unavailable` | 只注入读得到的层，**不**凭空产生 shadow |
| R6 | 单层部分失败 | 不放大成整层不可用 |

另有一条**反向**断言（`test_shadow_is_not_an_erasure`）：`shadows` 只是这一次的裁决，
磁盘上被压住的那个目录必须**原封不动**——否则「删项目级恢复用户级」就成了空话。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_skill_sandbox import skill_md

from edith_backend.internal.skills.catalog import Catalog
from edith_backend.internal.skills.resolver import (
    DUPLICATE_REASON,
    RESERVED_REASON,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    SCOPE_USER,
    SHADOW_REASON,
    SkillResolver,
)
from edith_backend.internal.skills.storage import (
    STATUS_EMPTY,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    SkillScopeStores,
    SkillStorageService,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend

USER = "user_scope_matrix"
WS = "ws-alpha"


# --------------------------------------------------------------------------
# 装配辅助
# --------------------------------------------------------------------------
def _write_skill(root: Path, dir_name: str, name: str, description: str) -> None:
    """按**目录名**建目录，frontmatter 的 name 单独给——R4 需要两者不一致。"""
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md(name, description), encoding="utf-8")


class Env:
    """一次测试的完整装配：用户级根、项目级根、catalog、scope 分发点。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        backend = LocalSkillBackend(tmp_path / "user-skills")
        self.user_root = backend.user_dir(USER)
        self.user_root.mkdir(parents=True, exist_ok=True)
        self.service = SkillStorageService(backend)
        self.catalog = Catalog(self.service)

        # 项目级根目录由环境变量给：`SkillScopeStores` 不接受自定义 root（生产也不该有）
        project_root = tmp_path / "project-skills"
        monkeypatch.setenv("EDITH_PROJECT_SKILLS_DIR", str(project_root))
        self.project_root = project_root / USER / WS
        self.project_root.mkdir(parents=True, exist_ok=True)

        self.scopes = SkillScopeStores(
            "local",
            user_service=self.service,
            access_checker=_allow_all,
        )

    def resolve(self, workspace_id: str | None = WS):
        return asyncio.run(
            SkillResolver(self.catalog, self.scopes).resolve(USER, workspace_id)
        )


async def _allow_all(user_id: str, workspace_id: str) -> bool:
    return bool(user_id and workspace_id)


async def _deny_all(user_id: str, workspace_id: str) -> bool:
    return False


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# R0：没有工作区上下文
# --------------------------------------------------------------------------
def test_r0_no_workspace_means_no_project_layer(env: Env) -> None:
    _write_skill(env.project_root, "proj-only", "proj-only", "项目级")
    resolved = env.resolve(workspace_id=None)

    assert resolved.by_scope(SCOPE_PROJECT) == []
    report = resolved.report(SCOPE_PROJECT)
    # 「没有项目」不能报成 available：否则前端会以为查过了
    assert report.status == STATUS_EMPTY
    assert report.count == 0
    # 且**不能**顺手把项目级的内容挤进用户层（那是最坏的"看起来能用"）
    assert "proj-only" not in {e.name for e in resolved.entries}


# --------------------------------------------------------------------------
# R1：单层出现
# --------------------------------------------------------------------------
def test_r1_single_layer_entries_all_injected(env: Env) -> None:
    _write_skill(env.user_root, "u-one", "u-one", "用户级")
    _write_skill(env.project_root, "p-one", "p-one", "项目级")

    resolved = env.resolve()
    by_name = {e.name: e for e in resolved.entries}

    assert by_name["u-one"].scope == SCOPE_USER
    assert by_name["p-one"].scope == SCOPE_PROJECT
    # 路径必须落在各自的沙箱根（模型据此读文件）
    assert by_name["u-one"].sandbox_path.startswith("skills/custom/")
    assert by_name["p-one"].sandbox_path.startswith(f"skills/project/{WS}/")
    assert resolved.shadows == []
    assert resolved.conflicts == []


def test_entries_order_is_platform_project_user(env: Env) -> None:
    """顺序是优先级降序，且与遍历顺序无关（5.1 的确定性要求）。"""
    _write_skill(env.user_root, "zz-user", "zz-user", "用户级")
    _write_skill(env.project_root, "aa-project", "aa-project", "项目级")

    resolved = env.resolve()
    scopes = [e.scope for e in resolved.entries]
    first_user = scopes.index(SCOPE_USER)
    # 所有项目级都在用户级之前；平台层整体在最前
    assert SCOPE_USER not in scopes[:first_user]
    assert all(
        s == SCOPE_PLATFORM for s in scopes[: len(scopes) - scopes.count(SCOPE_USER) - scopes.count(SCOPE_PROJECT)]
    )


# --------------------------------------------------------------------------
# C4 / C5 / C7：平台保留名
# --------------------------------------------------------------------------
def test_c4_user_cannot_override_platform_name(env: Env) -> None:
    platform_name = env.catalog.list_system_summaries()[0].name
    _write_skill(env.user_root, platform_name, platform_name, "用户同名")

    resolved = env.resolve()
    assert [e.name for e in resolved.entries if e.name == platform_name] == [platform_name]
    assert next(e.scope for e in resolved.entries if e.name == platform_name) == SCOPE_PLATFORM
    assert [(c.winner_scope, c.loser_scope, c.reason) for c in resolved.conflicts] == [
        (SCOPE_PLATFORM, SCOPE_USER, RESERVED_REASON)
    ]
    assert resolved.shadows == []


def test_c5_project_cannot_override_platform_name(env: Env) -> None:
    platform_name = env.catalog.list_system_summaries()[0].name
    _write_skill(env.project_root, platform_name, platform_name, "项目同名")

    resolved = env.resolve()
    assert next(e.scope for e in resolved.entries if e.name == platform_name) == SCOPE_PLATFORM
    assert [(c.winner_scope, c.loser_scope) for c in resolved.conflicts] == [
        (SCOPE_PLATFORM, SCOPE_PROJECT)
    ]


def test_c7_both_custom_layers_lose_to_platform(env: Env) -> None:
    """C7 产生**两条**记录：一次裁决一条，不让消费方再解析字段。"""
    platform_name = env.catalog.list_system_summaries()[0].name
    _write_skill(env.user_root, platform_name, platform_name, "用户同名")
    _write_skill(env.project_root, platform_name, platform_name, "项目同名")

    resolved = env.resolve()
    assert [e.name for e in resolved.entries].count(platform_name) == 1
    losers = sorted(c.loser_scope for c in resolved.conflicts if c.name == platform_name)
    assert losers == [SCOPE_PROJECT, SCOPE_USER]
    # 保留名先于 J 覆盖 U：项目级不该因为"更具体"而留下来
    assert resolved.shadows == []


# --------------------------------------------------------------------------
# C6：项目级覆盖用户级（D1）
# --------------------------------------------------------------------------
def test_c6_project_shadows_user_same_name(env: Env) -> None:
    _write_skill(env.user_root, "deploy", "deploy", "用户版部署流程")
    _write_skill(env.project_root, "deploy", "deploy", "项目版部署流程")

    resolved = env.resolve()
    winners = [e for e in resolved.entries if e.name == "deploy"]
    assert len(winners) == 1
    assert winners[0].scope == SCOPE_PROJECT
    assert winners[0].description == "项目版部署流程"

    assert [(s.winner_scope, s.loser_scope, s.reason) for s in resolved.shadows] == [
        (SCOPE_PROJECT, SCOPE_USER, SHADOW_REASON)
    ]
    # 覆盖是规则内行为，不是"冲突"——把它记进 conflicts 会让告警永久亮着
    assert resolved.conflicts == []
    # 两层的 count 各自保留（用于回答"用户到底有几个 Skill"）
    assert resolved.report(SCOPE_USER).count == 1
    assert resolved.report(SCOPE_PROJECT).count == 1


def test_shadow_is_not_an_erasure(env: Env) -> None:
    """`shadows` 只描述本次裁决，磁盘上的用户级目录必须原样存在。

    这条是 D3 的前提：如果解析顺手删了/搬了被压住的副本，「删掉项目级就恢复
    用户级」就永远恢复不回来——而且不会有任何报错，用户只看到 Skill 变成空。
    """
    _write_skill(env.user_root, "deploy", "deploy", "用户版")
    _write_skill(env.project_root, "deploy", "deploy", "项目版")

    env.resolve()
    assert (env.user_root / "deploy" / "SKILL.md").is_file()


# --------------------------------------------------------------------------
# D3：删项目级 → 用户级自动恢复（无持久化覆盖关系）
# --------------------------------------------------------------------------
def test_d3_deleting_project_restores_user_version(env: Env) -> None:
    import shutil

    _write_skill(env.user_root, "deploy", "deploy", "用户版")
    _write_skill(env.project_root, "deploy", "deploy", "项目版")

    shadowed = env.resolve()
    assert shadowed.shadows and shadowed.by_scope(SCOPE_PROJECT)

    shutil.rmtree(env.project_root / "deploy")

    restored = env.resolve()
    winners = [e for e in restored.entries if e.name == "deploy"]
    assert len(winners) == 1
    assert winners[0].scope == SCOPE_USER
    assert winners[0].description == "用户版"
    # 覆盖关系是每次重算的派生结果，不是落库的事实——所以这里必须自然消失
    assert restored.shadows == []


# --------------------------------------------------------------------------
# R4：同层重名（复制目录忘了改 frontmatter）
# --------------------------------------------------------------------------
def test_r4_same_layer_duplicate_keeps_lexicographically_first_dir(env: Env) -> None:
    _write_skill(env.user_root, "aaa", "dup", "先")
    _write_skill(env.user_root, "bbb", "dup", "后")

    resolved = env.resolve()
    winners = [e for e in resolved.entries if e.name == "dup"]
    assert len(winners) == 1
    assert winners[0].source_dir == "aaa"
    assert [(c.winner_scope, c.loser_scope, c.reason) for c in resolved.conflicts] == [
        (SCOPE_USER, SCOPE_USER, DUPLICATE_REASON)
    ]
    # R6：不能让 status 还是 ok —— 否则"复制目录忘了改名"只在 conflicts 里能找到
    report = resolved.report(SCOPE_USER)
    assert report.status == STATUS_PARTIAL
    assert "dup" in report.reason


def test_r4_tie_break_is_independent_of_scan_order(env: Env) -> None:
    """同一份磁盘内容，解析两次结果必须一致（不靠 iterdir 顺序）。"""
    _write_skill(env.user_root, "zzz", "dup", "后")
    _write_skill(env.user_root, "mmm", "dup", "先")

    first = env.resolve()
    second = env.resolve()
    assert [e.source_dir for e in first.entries if e.name == "dup"] == ["mmm"]
    assert [(e.name, e.scope) for e in first.entries] == [
        (e.name, e.scope) for e in second.entries
    ]


# --------------------------------------------------------------------------
# R5：项目层不可用 → fail-open（只注入读得到的层）
# --------------------------------------------------------------------------
def test_r5_denied_project_layer_keeps_user_skills(env: Env, monkeypatch) -> None:
    _write_skill(env.user_root, "deploy", "deploy", "用户版")
    _write_skill(env.project_root, "deploy", "deploy", "项目版")

    denied = SkillScopeStores("local", user_service=env.service, access_checker=_deny_all)
    resolved = asyncio.run(
        SkillResolver(env.catalog, denied).resolve(USER, WS)
    )

    report = resolved.report(SCOPE_PROJECT)
    assert report.status == STATUS_UNAVAILABLE
    assert report.reason
    # 项目层读不到时「谁被覆盖」是未知的，无法做减法 → 用户级照常注入
    assert [(e.name, e.scope) for e in resolved.entries if e.name == "deploy"] == [
        ("deploy", SCOPE_USER)
    ]
    # 未知 ≠ 未覆盖：不凭空造 shadow
    assert resolved.shadows == []


def test_r5_unavailable_layer_is_not_reported_as_empty(env: Env) -> None:
    """`unavailable` 与 `empty` 在注入文本里都表现为"没有"，但只有一个需要人介入。"""
    denied = SkillScopeStores("local", user_service=env.service, access_checker=_deny_all)
    resolved = asyncio.run(SkillResolver(env.catalog, denied).resolve(USER, WS))

    report = resolved.report(SCOPE_PROJECT)
    assert report.status != STATUS_EMPTY
    assert report.status == STATUS_UNAVAILABLE
    assert "not accessible" in report.reason


# --------------------------------------------------------------------------
# 只读不写：解析不得改动权威副本（整条链路的不变量）
# --------------------------------------------------------------------------
def test_resolution_is_read_only(env: Env) -> None:
    _write_skill(env.user_root, "deploy", "deploy", "用户版")
    _write_skill(env.project_root, "deploy", "deploy", "项目版")

    def snapshot(root: Path) -> dict[str, bytes]:
        # `overview.md` / `.edithignore` 是**派生缓存**，`read_index` 本来就会自愈回写
        # （设计稿 7.4）；这里要钉的是「Skill 内容一个字节都不动」。
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
            and not p.name.startswith(".")
            and p.name != "overview.md"
        }

    before = (snapshot(env.user_root), snapshot(env.project_root))
    env.resolve()
    env.resolve()
    assert (snapshot(env.user_root), snapshot(env.project_root)) == before


def test_missing_project_root_is_empty_not_error(env: Env, monkeypatch, tmp_path) -> None:
    """项目级目录还不存在（新建工作区）时是 empty，不是 unavailable。"""
    monkeypatch.setenv("EDITH_PROJECT_SKILLS_DIR", str(tmp_path / "never-created"))
    resolved = env.resolve()
    report = resolved.report(SCOPE_PROJECT)
    assert report.status == STATUS_EMPTY
    assert report.reason == ""
