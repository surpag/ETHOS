"""Skill 落点：模型写到哪个路径，就决定它属于哪个命名空间。

背景（2026-11-09 事故）
-----------------------
平台 Skill 与用户项目都叫 `skills`，但前者是沙箱根下的 `skills/custom`、
`skills/project/{ws}`，后者是 `workspace/{ws}/skills/`。注入文本原先只写
「完整正文位于 …」，没说「新建该写哪」，于是「在这个项目里建个 Skill」
被写进了 `workspace/{ws}/skills/` —— 没有报错、也没有被加载，界面上却显示
「创建成功」（实际是 `rounds_exhausted`，计划完成度被当成了交付结果）。

这组测试守的是**落点**这一层事实：同一个写入动作，路径不同就必须落到不同的
权威副本，且两边永不串味。它不检验模型的判断力（那需要真实 LLM 会话）。
"""

from __future__ import annotations

import asyncio

import pytest
from fake_skill_sandbox import FakeSkillSandbox, skill_md

from edith_backend.internal.sandbox.service import Service as SandboxService
from edith_backend.internal.skills.storage import (
    SkillScopeStores,
    SkillStorageService,
)
from edith_backend.internal.skills.storage.local import LocalSkillBackend

USER = "user_write_target"
WS = "ws-target"


async def _allow(_user_id: str, _workspace_id: str) -> bool:
    return True


@pytest.fixture()
def sandboxes(tmp_path, monkeypatch) -> tuple[SandboxService, object, object]:
    """返回 (service, user_skills_root, project_skills_root)。"""
    monkeypatch.setenv("EDITH_PROJECT_SKILLS_DIR", str(tmp_path / "project-skills"))
    user_root = tmp_path / "user-skills"
    service = SkillStorageService(LocalSkillBackend(user_root))
    stores = SkillScopeStores("local", user_service=service, access_checker=_allow)
    return (
        SandboxService(None, "template", None, skill_scopes=stores),  # type: ignore[arg-type]
        user_root,
        tmp_path / "project-skills",
    )


def test_user_scoped_write_lands_in_user_store(sandboxes) -> None:
    """写 `skills/custom/<name>/` → 宿主 `user-skills/{uid}/<name>/`。"""

    async def go() -> None:
        service, user_root, project_root = sandboxes
        sandbox = FakeSkillSandbox("sbx")
        sandbox.files.write_now(
            "/home/user/skills/custom/demo/SKILL.md",
            skill_md("demo", "演示").encode(),
        )
        await service.pull_skills(USER, WS, sandbox)

        assert (user_root / USER / "demo" / "SKILL.md").is_file()
        # 用户级不得写进项目权威根
        assert not (project_root / USER / WS / "demo").exists()

    asyncio.run(go())


def test_project_scoped_write_lands_in_project_store(sandboxes) -> None:
    """写 `skills/project/{ws}/<name>/` → 宿主 `project-skills/{uid}/{ws}/<name>/`。"""

    async def go() -> None:
        service, user_root, project_root = sandboxes
        sandbox = FakeSkillSandbox("sbx")
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS}/proj/SKILL.md",
            skill_md("proj", "项目级").encode(),
        )
        await service.pull_skills(USER, WS, sandbox)

        assert (project_root / USER / WS / "proj" / "SKILL.md").is_file()
        assert not (user_root / USER / "proj").exists()

    asyncio.run(go())


def test_project_workspace_skills_dir_never_becomes_a_platform_skill(sandboxes) -> None:
    """写 `workspace/{ws}/skills/<name>/` = 项目文件，**不得**被当成平台 Skill。

    这是事故的原始形态：模型以为「项目里的 skills/」就是 EDITH Skill 目录。
    必须验证它两边权威副本都不出现 —— 也就是说那次「创建成功」其实什么都没创建。
    """

    async def go() -> None:
        service, user_root, project_root = sandboxes
        sandbox = FakeSkillSandbox("sbx")
        sandbox.files.write_now(
            f"/home/user/workspace/{WS}/skills/hello-test/SKILL.md",
            skill_md("hello-test", "被写错位置").encode(),
        )
        await service.pull_skills(USER, WS, sandbox)

        assert not (user_root / USER / "hello-test").exists()
        assert not (project_root / USER / WS / "hello-test").exists()
        # 文件本身还在项目目录里（它是项目文件，不是丢了）
        assert (
            f"/home/user/workspace/{WS}/skills/hello-test/SKILL.md"
            in sandbox.files.blobs
        )

    asyncio.run(go())


def test_both_scopes_coexist_without_overwriting_each_other(sandboxes) -> None:
    """同名 Skill 分两层写入 → 两个权威副本各自存在（覆盖靠 resolver 裁决，不靠落点）。"""

    async def go() -> None:
        service, user_root, project_root = sandboxes
        sandbox = FakeSkillSandbox("sbx")
        sandbox.files.write_now(
            "/home/user/skills/custom/same/SKILL.md",
            skill_md("same", "用户级版本").encode(),
        )
        sandbox.files.write_now(
            f"/home/user/skills/project/{WS}/same/SKILL.md",
            skill_md("same", "项目级版本").encode(),
        )
        await service.pull_skills(USER, WS, sandbox)

        user_copy = (user_root / USER / "same" / "SKILL.md").read_text(encoding="utf-8")
        project_copy = (
            project_root / USER / WS / "same" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "用户级版本" in user_copy
        assert "项目级版本" in project_copy

    asyncio.run(go())
