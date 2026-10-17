"""skills 目录：加载内置 Skill 与解析用户 overview.md。

对应 Go 版 internal/skills/catalog.go。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .types import Skill, SkillSummary

_SYSTEM_DIR = Path(__file__).parent / "system"


def _split_frontmatter(content: str) -> tuple[str, str]:
    """分离 SKILL.md 的 YAML 头部和 Markdown 正文。"""
    content = content.lstrip("\uFEFF")
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError("SKILL.md frontmatter is not closed")


def _load_skill(skill_dir: Path) -> Skill:
    skill_path = skill_dir / "SKILL.md"
    data = skill_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(data)
    metadata = yaml.safe_load(frontmatter) or {}
    name = (metadata.get("name") or "").strip()
    description = (metadata.get("description") or "").strip()
    if not name or not description:
        raise ValueError(f"{skill_path} frontmatter requires name and description")

    display_name = name
    short_description = description
    edith_path = skill_dir / "edith.yaml"
    if edith_path.exists():
        edith = yaml.safe_load(edith_path.read_text(encoding="utf-8")) or {}
        display_name = (edith.get("display_name") or "").strip() or name
        short_description = (edith.get("short_description") or "").strip() or description

    return Skill(
        id=skill_dir.name,
        name=name,
        description=description,
        body=body.strip(),
        display_name=display_name,
        short_description=short_description,
    )


def load_catalog() -> list[Skill]:
    """扫描并加载内置 Skills，返回按名称排序的列表。"""
    skills: list[Skill] = []
    for skill_dir in sorted(_SYSTEM_DIR.iterdir()):
        if not (skill_dir / "SKILL.md").exists():
            continue
        skills.append(_load_skill(skill_dir))
    skills.sort(key=lambda s: s.name)
    return skills


def parse_user_overview(content: str) -> list[SkillSummary]:
    """解析 sync_overview.py 生成的 Skill 摘要列表。"""
    result: list[SkillSummary] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line.startswith("- `"):
            continue
        remaining = line[3:]
        end_name = remaining.find("`")
        if end_name <= 0:
            continue
        name = remaining[:end_name].strip()
        description = remaining[end_name + 1 :].strip()
        description = description.removeprefix("：").removeprefix(":").strip()
        if not name or not description:
            continue
        result.append(SkillSummary(name=name, description=description))
    result.sort(key=lambda s: s.name)
    return result


class Catalog:
    """提供内置 Skill 摘要与用户 overview 读取。"""

    def __init__(self, volumes=None) -> None:
        self._skills = load_catalog()
        self._volumes = volumes

    def list_system_summaries(self) -> list[SkillSummary]:
        """返回按稳定顺序排列的内置 Skill 摘要。"""
        return [SkillSummary(name=s.name, description=s.description) for s in self._skills]

    async def list_user_summaries(self, user_id: str) -> list[SkillSummary]:
        """返回当前用户 overview.md 中的自定义 Skill 摘要。"""
        if self._volumes is None:
            return []
        overview = await self._volumes.read_user_overview(user_id)
        return parse_user_overview(overview)

    async def read_user_overview(self, user_id: str) -> str:
        """返回当前用户的自定义 Skill 摘要原文。"""
        if self._volumes is None:
            return ""
        return await self._volumes.read_user_overview(user_id)
