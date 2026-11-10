"""注入格式 golden（B2-3 新格式）与解析层的装配校验。

为什么这个文件被重写了
----------------------
B0 时这里是一个**逐字节对比**的 oracle：把改造前的渲染函数原样冻结，与新实现做
`==`，用来证明「收敛解析点没改变任何一个字符」。

B2-3 是**刻意**改变输出的那一步（两段小标题 → 带 scope 标签的统一列表，渲染来源
从索引原文改成裁决后的 `entries`）。oracle 一旦被"顺手对齐"成新实现就退化成
同义反复，所以这里改成「新格式 golden」：把期望文本**写死**，任何格式漂移都会被
逐字符抓住，而不是只断言 `"demo" in text`。

仍然保留的两类性质（与格式无关）：
- 解析产物的 scope / sandbox_path 必须与索引路径同源；
- 空 catalog 必须产出空串（否则每轮 prompt 都会长出一段没内容的说明）。
"""

from __future__ import annotations

import asyncio

from fake_skill_sandbox import skill_md

from edith_backend.internal.agentrun.configuration import (
    render_skill_instruction,
    run_instruction,
    skill_instruction,
)
from edith_backend.internal.skills.budget import index_budget_chars
from edith_backend.internal.skills.catalog import Catalog
from edith_backend.internal.skills.resolver import (
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    SCOPE_USER,
    ResolvedCatalog,
    ResolvedEntry,
    SkillResolver,
)
from edith_backend.internal.skills.storage import STATUS_EMPTY, SkillStorageService
from edith_backend.internal.skills.storage.local import LocalSkillBackend
from edith_backend.internal.skills.types import CUSTOM_PATH, SYSTEM_PATH

USER = "user_resolver_golden"

#: 路径说明段落的**冻结期望**（写死，不 import 实现里的常量）。
#: 它每轮都进 system prompt 末尾，逐字节漂移会同时打断前缀缓存与「模型知道往哪写」，
#: 所以这里刻意逐字符比对，而不是 `assert "skills/custom" in text`。
EXPECTED_PATH_GUIDE = (
    "## EDITH Skill 存储位置（与项目目录无关）\n"
    "以下目录属于 EDITH 平台，不是用户项目的一部分。它们位于 Sandbox 根目录 /home/user 下，"
    "在文件工具里用**相对路径**操作：\n"
    "- 平台 Skills（只读）：skills/system/<目录>/SKILL.md\n"
    "- 项目级 Skills（属于当前会话工作区）：skills/project/<当前工作区>/<目录>/SKILL.md\n"
    "- 用户级 Skills（跨工作区共享）：skills/custom/<目录>/SKILL.md\n"
    "\n"
    "创建或修改 EDITH Skill 时：\n"
    "- 先判断作用域。用户级写 skills/custom/<skill-name>/；"
    "项目级写 skills/project/<当前工作区>/<skill-name>/。\n"
    "- 「在这个项目里」「给这个仓库加个 skill」**不等于**项目级 Skill："
    "只有用户明确要求「项目级 Skill」或限定只在当前工作区生效时才用 project 层，否则一律用户级。\n"
    "- 绝不把 EDITH Skill 写进 workspace/<当前工作区>/skills/：那是用户项目自己的 skills 目录，"
    "与平台 Skill 存储是两个命名空间，写错不会报错但也永远不会被加载。\n"
    "- 创建 EDITH Skill 时不要顺带改项目的 registry、package.json、CI 或文档，"
    "除非用户明确要求做项目改动。\n"
    "- 写完必须用文件工具读回目标 SKILL.md，确认文件真实存在且内容正确，再向用户报告创建成功。"
)


# --------------------------------------------------------------------------
# 一、渲染层：新格式逐字符比对
# --------------------------------------------------------------------------
def _entry(
    name: str, scope: str, description: str = "说明", dir_name: str = ""
) -> ResolvedEntry:
    source_dir = dir_name or name
    if scope == SCOPE_PLATFORM:
        base = SYSTEM_PATH
    elif scope == SCOPE_PROJECT:
        base = "skills/project/ws-1"
    else:
        base = CUSTOM_PATH
    return ResolvedEntry(
        name=name,
        description=description,
        scope=scope,
        sandbox_path=f"{base}/{source_dir}",
        source_dir=source_dir,
    )


def test_empty_catalog_renders_nothing() -> None:
    """没有 Skill 时**一个字都不输出**：空段落会让每轮 prompt 都白长一截。"""
    assert render_skill_instruction(ResolvedCatalog()).text == ""
    assert skill_instruction(ResolvedCatalog()) == ""


def test_single_platform_entry_exact_text(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(
        entries=[
            _entry("current-time", SCOPE_PLATFORM, "需要知道当前时间时，调用 get_current_time 工具。")
        ]
    )
    expected = (
        "可用 Skills（按作用域从高到低排列，同名时靠前者生效）：\n"
        "- [platform] current-time：需要知道当前时间时，调用 get_current_time 工具。\n"
        "\n"
        + EXPECTED_PATH_GUIDE
        + "\n"
        "同一轮内最多读取 3 个 Skill 的完整正文（累计 60KB）；超出会被拒绝，"
        "优先依据上面的摘要行动。"
    )
    assert render_skill_instruction(catalog).text == expected
    # 兼容包装必须返回同一份文本
    assert skill_instruction(catalog) == expected


def test_three_scopes_order_and_labels(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(
        entries=[
            _entry("p-skill", SCOPE_PLATFORM),
            _entry("j-skill", SCOPE_PROJECT),
            _entry("u-skill", SCOPE_USER),
        ]
    )
    rendered = render_skill_instruction(catalog)
    lines = rendered.text.splitlines()
    assert lines[1] == "- [platform] p-skill：说明"
    assert lines[2] == "- [project] j-skill：说明"
    assert lines[3] == "- [user] u-skill：说明"
    # 三个作用域的路径必须给全（少一个就是"模型读不到那层"）。
    # 路径现在是固定段落，不再随出现的作用域收窄 —— 用户级为空时也必须写出来，
    # 否则「第一次要建 Skill」这个最需要写清的场景恰好没有指引。
    assert f"{SYSTEM_PATH}/<目录>/SKILL.md" in rendered.text
    assert "skills/project/<当前工作区>/<目录>/SKILL.md" in rendered.text
    assert f"{CUSTOM_PATH}/<目录>/SKILL.md" in rendered.text
    assert rendered.injected == [
        f"{SCOPE_PLATFORM}:p-skill",
        f"{SCOPE_PROJECT}:j-skill",
        f"{SCOPE_USER}:u-skill",
    ]
    assert rendered.omitted == []


def test_path_guide_separates_platform_storage_from_project_skills(monkeypatch) -> None:
    """平台 Skill 与项目的 `skills/` 是两个命名空间，注入必须说破。

    这条守的是真实事故：模型把「在这个项目里建个 skill」写成了
    `workspace/<ws>/skills/<name>/`（项目目录），没有报错、也没有被加载，
    界面上看起来是「创建成功」。只要这段提示被删掉，事故就会复现。
    """
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    text = render_skill_instruction(ResolvedCatalog(entries=[_entry("a", SCOPE_USER)])).text

    assert "workspace/<当前工作区>/skills/" in text
    assert "绝不把 EDITH Skill 写进" in text
    # 「在这个项目里」不得被当成项目级的依据
    assert "不等于" in text and "否则一律用户级" in text
    # 不许顺带改项目文件（registry / CI / 文档）
    assert "package.json" in text
    # 创建后必须读回验证，而不是凭"我写完了"宣告成功
    assert "读回" in text and "确认文件真实存在" in text


def test_path_guide_is_byte_stable(monkeypatch) -> None:
    """固定段落必须逐字节稳定：它是 system prompt 末尾的前缀缓存锚点。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(entries=[_entry("a", SCOPE_USER)])
    first = render_skill_instruction(catalog).text
    second = render_skill_instruction(catalog).text
    assert first == second
    # 整段必须原样出现一次（不是"包含关键词"）
    assert first.count(EXPECTED_PATH_GUIDE) == 1


def test_guide_fills_real_workspace_id_when_available(monkeypatch) -> None:
    """有工作区时必须写真实 ID：占位符会被模型照抄成路径。

    照抄的后果不是"落在别处"，而是 `workspace_path` 判第二段不等于当前工作区 →
    `PermissionError` → 模型换个能写的路径重试 → 最终落回 `skills/custom`。
    表现正是「明确要求项目级，结果变成用户级」。
    """
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(entries=[_entry("a", SCOPE_USER)])

    text = render_skill_instruction(catalog, "ws-abc123").text
    assert "skills/project/ws-abc123/<目录>/SKILL.md" in text
    assert "skills/project/ws-abc123/<skill-name>/" in text
    assert "workspace/ws-abc123/skills/" in text
    assert "<当前工作区>" not in text

    # 没有工作区上下文时保留占位符：那时项目级本来就不可写
    fallback = render_skill_instruction(catalog).text
    assert "<当前工作区>" in fallback


def test_dir_name_is_exposed_only_when_it_differs(monkeypatch) -> None:
    """目录名与 name 不一致时必须写出来：`sandbox_path` 是用目录名拼的。

    只写 name 的失败形态是「模型读一个不存在的目录」——它不报错，读到的是空内容。
    """
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(
        entries=[
            _entry("deploy", SCOPE_USER, "部署", dir_name="deploy-flow"),
            _entry("plain", SCOPE_USER, "普通"),
        ]
    )
    lines = render_skill_instruction(catalog).text.splitlines()
    assert lines[1] == "- [user] deploy：部署（目录：deploy-flow）"
    # 一致时不写，省下的都是预算
    assert lines[2] == "- [user] plain：普通"


def test_budget_omission_is_stated_in_text_and_accounted(monkeypatch) -> None:
    """超出 L1 预算时：文本里写数量、账目里有名字（缺一边就是静默丢内容）。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "600")
    entries = [_entry(f"skill-{i:02d}", SCOPE_USER, "很长的说明" * 5) for i in range(20)]
    rendered = render_skill_instruction(ResolvedCatalog(entries=entries))

    omitted = len(rendered.omitted)
    assert omitted > 0
    assert f"另有 {omitted} 个 Skill" in rendered.text
    assert rendered.budget_chars == 600
    # 被省略的不是随机的一半：一定是靠后的那些（顺序 = 优先级 + name）
    assert rendered.omitted == [f"{SCOPE_USER}:skill-{i:02d}" for i in range(len(rendered.injected), 20)]


def test_render_is_deterministic(monkeypatch) -> None:
    """同一份 entries 渲染两次必须逐字符相同（前缀缓存的前提）。"""
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    entries = [_entry(f"s{i:02d}", SCOPE_USER) for i in range(10)]
    catalog = ResolvedCatalog(entries=entries)
    assert render_skill_instruction(catalog).text == render_skill_instruction(catalog).text


def test_run_instruction_appends_uploads_after_skills(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    catalog = ResolvedCatalog(entries=[_entry("demo", SCOPE_USER)])
    text = run_instruction(catalog, ["uploads/a.pdf"])
    assert text.startswith("可用 Skills")
    assert "本次用户上传文件：\n- uploads/a.pdf" in text
    assert text.index("可用 Skills") < text.index("本次用户上传文件")


def test_run_instruction_without_skills_still_lists_uploads() -> None:
    text = run_instruction(ResolvedCatalog(), ["uploads/a.pdf"])
    assert text.startswith("本次用户上传文件")


# --------------------------------------------------------------------------
# 二、装配层：真实平台 Skill + 真实本地存储
# --------------------------------------------------------------------------
def _write_skill(root, name: str, description: str, dir_name: str = "") -> None:
    skill_dir = root / (dir_name or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md(name, description), encoding="utf-8")


def test_entries_carry_scope_and_sandbox_path(tmp_path) -> None:
    backend = LocalSkillBackend(tmp_path / "user-skills")
    catalog = Catalog(SkillStorageService(backend))
    _write_skill(backend.user_dir(USER), "demo", "演示用")

    resolved = asyncio.run(SkillResolver(catalog).resolve(USER))
    by_name = {e.name: e for e in resolved.entries}
    assert by_name["demo"].scope == SCOPE_USER
    # 路径必须与索引文本里写的一致，否则模型会拿索引去读一个不存在的目录
    assert by_name["demo"].sandbox_path == f"{CUSTOM_PATH}/demo"
    platform = resolved.by_scope(SCOPE_PLATFORM)[0]
    assert platform.sandbox_path == f"{SYSTEM_PATH}/{platform.source_dir}"


def test_real_catalog_renders_all_scopes_without_overview(monkeypatch, tmp_path) -> None:
    """真实数据：平台层 + 用户层都进 entries；注入不再读索引原文。

    B2-3 的关键断言是**索引原文里有的东西不再原样出现**：旧实现直接把
    `overview.md` 的正文（含 `# 用户 Skills 总览`、`- 路径：...` 这些行）塞进 prompt，
    现在只出现条目行。这条如果回退，缓存会悄悄多出几百字符的噪声。
    """
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    backend = LocalSkillBackend(tmp_path / "user-skills")
    catalog = Catalog(SkillStorageService(backend))
    _write_skill(backend.user_dir(USER), "demo", "演示用")
    _write_skill(backend.user_dir(USER), "note-taker", "记笔记")

    resolved = asyncio.run(SkillResolver(catalog).resolve(USER))
    rendered = render_skill_instruction(resolved)

    assert [e.name for e in resolved.by_scope(SCOPE_USER)] == ["demo", "note-taker"]
    assert "- [user] demo：演示用" in rendered.text
    assert "- [user] note-taker：记笔记" in rendered.text
    # 索引原文的结构标记不得再出现
    assert "用户 Skills 总览" not in rendered.text
    assert "路径：`skills/custom" not in rendered.text
    # 平台层同样被渲染（不是"只有用户层"）
    assert any(e.scope == SCOPE_PLATFORM for e in resolved.entries)
    assert "- [platform]" in rendered.text


def test_user_report_status_passthrough(tmp_path) -> None:
    backend = LocalSkillBackend(tmp_path / "user-skills")
    catalog = Catalog(SkillStorageService(backend))

    report = asyncio.run(SkillResolver(catalog).resolve(USER)).report(SCOPE_USER)
    # 空目录 = empty，不是 unavailable：两者注入文本都为空，但只有后者是故障
    assert report.status == STATUS_EMPTY
    assert report.count == 0


def test_missing_storage_surfaces_reason() -> None:
    """没有配置存储时，reason 必须能被看到，而不是被压成空字符串。"""
    catalog = Catalog(None)
    resolved = asyncio.run(SkillResolver(catalog).resolve(USER))
    report = resolved.report(SCOPE_USER)
    assert report.status == STATUS_EMPTY
    assert "not configured" in report.reason
    # 仍然能渲染（平台层存在），且不会被"存储缺失"打断
    assert render_skill_instruction(resolved).text


def test_workspace_id_without_scope_stores_reads_nothing(tmp_path) -> None:
    """没有作用域分发点时，传 workspace_id 不得凭空读出一层项目 Skill。

    这条守的是「配置缺失 ≠ 读到一个空项目」：项目层必须能被报告成
    `empty/not configured`，而绝不是"悄悄去读了某个默认根目录"。
    """
    backend = LocalSkillBackend(tmp_path / "user-skills")
    catalog = Catalog(SkillStorageService(backend))
    _write_skill(backend.user_dir(USER), "demo", "演示用")

    resolver = SkillResolver(catalog)
    plain = asyncio.run(resolver.resolve(USER))
    with_ws = asyncio.run(resolver.resolve(USER, "ws-anything"))
    assert skill_instruction(plain) == skill_instruction(with_ws)
    assert [e.name for e in plain.entries] == [e.name for e in with_ws.entries]
    assert with_ws.by_scope(SCOPE_PROJECT) == []
    assert "not configured" in with_ws.report(SCOPE_PROJECT).reason


def test_reserved_name_conflict_text_shows_only_winner(monkeypatch, tmp_path) -> None:
    """B2-2 之前的过渡态（被压住的那条仍列在注入里）在 B2-3 消失。

    这是「注入与裁决同源」的直接后果：同名只出现一次，且是平台那一条。
    """
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "4000")
    backend = LocalSkillBackend(tmp_path / "user-skills")
    catalog = Catalog(SkillStorageService(backend))
    platform_name = catalog.list_system_summaries()[0].name
    _write_skill(backend.user_dir(USER), platform_name, "用户自己写的同名 Skill", dir_name="user-copy")

    resolved = asyncio.run(SkillResolver(catalog).resolve(USER))
    rendered = render_skill_instruction(resolved)

    assert rendered.text.count(f" {platform_name}：") == 1
    assert "- [platform]" in rendered.text
    assert "- [user]" not in rendered.text
    # 冲突仍然被记录（诊断面不受渲染影响）
    assert [c.loser_scope for c in resolved.conflicts] == [SCOPE_USER]


def test_budget_chars_reported_always(monkeypatch) -> None:
    monkeypatch.setenv("SKILL_INDEX_BUDGET_CHARS", "1234")
    assert index_budget_chars() == 1234
    rendered = render_skill_instruction(ResolvedCatalog(entries=[_entry("a", SCOPE_USER)]))
    assert rendered.budget_chars == 1234
