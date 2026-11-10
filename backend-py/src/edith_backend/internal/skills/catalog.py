"""skills 目录：加载内置 Skill 与用户 Skill 索引。

对应 Go 版 internal/skills/catalog.go。

用户 Skill 的存储从「只读 E2B Volume 的 overview.md」改为「经存储门面读取」：
后端的权威副本可能是宿主目录（Local）或卷（E2B），由 `storage` 决定，
本模块不感知。`read_user_overview` 的签名与返回形态保持不变（仍是索引原文），
因此注入到 System Instruction 的文本零变化。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .storage.base import STATUS_EMPTY, SkillIndex
from .types import Skill, SkillSummary

logger = logging.getLogger(__name__)

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
    """提供内置 Skill 摘要与用户 Skill 索引读取。"""

    def __init__(self, storage=None) -> None:
        self._skills = load_catalog()
        # 存储门面（SkillStorageService）。None = 未配置存储，按「没有用户 Skill」处理，
        # 但要带上原因，不能假装「读到了空目录」。
        self._storage = storage

    def list_system_summaries(self) -> list[SkillSummary]:
        """返回按稳定顺序排列的内置 Skill 摘要。"""
        return [
            SkillSummary(name=s.name, description=s.description, dir_name=s.id)
            for s in self._skills
        ]

    async def read_index(self, user_id: str) -> SkillIndex:
        """读取用户 Skill 索引（含状态与失败原因）。"""
        if self._storage is None:
            return SkillIndex(status=STATUS_EMPTY, reason="skill storage not configured")
        return await self._storage.read_index(user_id)

    async def list_user_summaries(self, user_id: str) -> list[SkillSummary]:
        """返回当前用户自定义 Skill 摘要。"""
        index = await self.read_index(user_id)
        # 后端可能只给出索引原文（E2B 卷内就是一份 markdown），
        # 结构化摘要缺失时从原文解析，两条路得到同一份列表。
        return index.summaries or parse_user_overview(index.text)

    async def read_user_overview(self, user_id: str) -> str:
        """返回当前用户的自定义 Skill 索引原文（保持旧签名，注入侧不用改）。"""
        return (await self.read_index(user_id)).text
