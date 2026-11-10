"""skills 的 HTTP 边界。

对应 Go 版 internal/skills/http.go。

`/internal/skills` 的扩展方式（B2-2）：**只加字段，不改既有字段语义**。

- `custom` 仍是「你拥有的用户 Skill」，不随 `workspaceId` 变化、也不做覆盖裁决。
  把它改成裁决后的结果会让「设置页里我突然少了几条」看起来像丢数据；
  要知道「这个项目里哪条生效」，看 `shadows`；
- 传 `workspaceId` 才返回 `project` / `projectStatus` / `shadows` / `conflicts`。
  不传时这几项为空值——没有项目上下文时「项目 Skill」不存在（R0），不是「查不到」。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..http_utils import EDITHError
from .catalog import Catalog, parse_user_overview
from .resolver import SCOPE_PROJECT, SkillResolver
from .types import SkillListItem, SkillsResponse


class HTTP:
    def __init__(self, catalog: Catalog, scopes=None) -> None:
        self.catalog = catalog
        # 作用域分发点：项目级索引只能经它读（归属判定在存储层，HTTP 不碰权限逻辑）
        self.resolver = SkillResolver(catalog, scopes)
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/skills", response_model=SkillsResponse)
        async def list_skills(
            userId: str = "", workspaceId: str = ""
        ) -> SkillsResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            index = await self.catalog.read_index(userId)
            custom = index.summaries or parse_user_overview(index.text)
            resolved = await self.resolver.resolve(userId, workspaceId)
            project_report = resolved.report(SCOPE_PROJECT)
            return SkillsResponse(
                system=[
                    SkillListItem(name=s.name, description=s.description)
                    for s in self.catalog.list_system_summaries()
                ],
                custom=[
                    SkillListItem(name=s.name, description=s.description) for s in custom
                ],
                customStatus=index.status,
                customReason=index.reason,
                project=[
                    SkillListItem(name=e.name, description=e.description)
                    for e in resolved.by_scope(SCOPE_PROJECT)
                ],
                projectStatus=project_report.status,
                projectReason=project_report.reason,
                shadows=[_decision_view(item) for item in resolved.shadows],
                conflicts=[_decision_view(item) for item in resolved.conflicts],
            )

        return router


def _decision_view(decision) -> dict:
    """与 trace 同一份形状，避免前端为两个接口写两套解构。"""
    view = {
        "name": decision.name,
        "winner": decision.winner_scope,
        "loser": decision.loser_scope,
        "reason": decision.reason,
    }
    if decision.detail:
        view["detail"] = decision.detail
    return view
