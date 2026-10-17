"""skills 的类型定义。

对应 Go 版 internal/skills/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

# Sandbox 文件工具使用的相对路径
SYSTEM_PATH = "skills/system"
CUSTOM_PATH = "skills/custom"


@dataclass
class Skill:
    """一个已经解析完成的 Skill。"""

    id: str
    name: str
    description: str
    body: str = ""
    display_name: str = ""
    short_description: str = ""


@dataclass
class SkillSummary:
    """传给 AgentRun 的最小 Skill 描述。"""

    name: str
    description: str


class SkillListItem(BaseModel):
    """浏览器展示的 Skill 摘要。"""

    name: str
    description: str


class SkillsResponse(BaseModel):
    """Skills 列表 HTTP 输出。"""

    system: list[SkillListItem]
    custom: list[SkillListItem]
