"""skills 的 HTTP 边界。

对应 Go 版 internal/skills/http.go。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..http_utils import EDITHError
from .catalog import Catalog
from .types import SkillListItem, SkillsResponse


class HTTP:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/internal/skills", response_model=SkillsResponse)
        async def list_skills(userId: str = "") -> SkillsResponse:
            if not userId.strip():
                raise EDITHError(400, "invalid_request", "userId is required")
            custom = await self.catalog.list_user_summaries(userId)
            return SkillsResponse(
                system=[SkillListItem(name=s.name, description=s.description) for s in self.catalog.list_system_summaries()],
                custom=[SkillListItem(name=s.name, description=s.description) for s in custom],
            )

        return router
