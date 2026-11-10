"""skills 的类型定义。

对应 Go 版 internal/skills/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

# Sandbox 文件工具使用的相对路径
SYSTEM_PATH = "skills/system"
CUSTOM_PATH = "skills/custom"
# 项目级 Skill 的父目录。真正的目录还带一层 workspace_id（`skills/project/{workspace_id}/{name}`）：
# 沙箱按 (user, session) 复用而不是按 workspace，固定路径会让同一沙箱先后服务两个项目时
# 出现「索引指向 A、文件是 B」。带上 ID 之后，这个问题在结构上不可表达（见设计稿 7.2）。
PROJECT_PATH = "skills/project"

# 作用域取值。定义在 types 而不是 resolver：存储层（`storage/scopes.py`）也要用，
# 而 resolver 依赖存储层——放在 resolver 会形成 storage → resolver → catalog → storage 的环。
# 用字符串而非 Enum：它要直接进 trace / HTTP / 日志，Enum 在序列化边界会被反复转换。
SCOPE_PLATFORM = "platform"
SCOPE_USER = "user"
SCOPE_PROJECT = "project"

# 已实现的作用域（解析器按此顺序读取与裁决）。B2-2 起包含项目级：
# 只有传了 `workspace_id` 才会真的去读它（R0），没有工作区时项目层不存在。
IMPLEMENTED_SCOPES = (SCOPE_PLATFORM, SCOPE_USER, SCOPE_PROJECT)


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
    """传给 AgentRun 的最小 Skill 描述。

    `dir_name` 是权威副本里的**真实目录名**（与 frontmatter 的 `name` 分开）：
    索引的「路径」行、`ResolvedEntry.sandbox_path` 都必须用目录名，否则
    「目录名 ≠ name」时模型会拿到一个不存在的路径（读到的是空 Skill，不是报错）。
    留空表示「来源不知道目录名」（如 e2b 卷里只有 markdown 索引），此时退回 name。
    """

    name: str
    description: str
    dir_name: str = ""


class SkillListItem(BaseModel):
    """浏览器展示的 Skill 摘要。"""

    name: str
    description: str


class SkillsResponse(BaseModel):
    """Skills 列表 HTTP 输出。

    customStatus / customReason 是新增的可观测字段（向后兼容：老客户端忽略即可）：
    `custom` 为空数组时，前端需要能区分「用户没有 Skill」与「存储不可用」——
    两者在旧实现里都只是一个空列表，故障因此长期隐形。

    `project*` / `shadows` / `conflicts` 同理（B2-2 起）。两个语义边界：

    - `custom` 是「**你拥有**的用户 Skill」，不随项目变化。它**不**做覆盖裁决，
      否则设置页里会突然少掉几条，看起来像丢数据；
    - `shadows` 说明「这个项目里哪几条被项目级压住了」。要判断「此刻哪一条生效」，
      看 `shadows` 而不是在 `custom` 里筛。
    """

    system: list[SkillListItem]
    custom: list[SkillListItem]
    customStatus: str = ""
    customReason: str = ""
    project: list[SkillListItem] = Field(default_factory=list)
    projectStatus: str = ""
    projectReason: str = ""
    #: [{"name","winner","loser","reason","detail"?}]，见 resolver.ScopeDecision
    shadows: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
