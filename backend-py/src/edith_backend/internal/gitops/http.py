"""gitops 的 HTTP 边界：把工作区提交推送到绑定仓库。

编排（沙箱导出 bundle → 后端带凭据 push）在 `orchestrator.WorkspacePush`，
本模块只负责**参数校验 + 错误码到 HTTP 状态的翻译**，与 Agent 工具 `git_push`
共用同一份实现（不在两处各写一遍推送流程）。

注意：`gitops/__init__.py` **不导出本模块**，避免 `sandbox ↔ gitops` 循环导入
（sandbox 导 gitops，本模块导 sandbox）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ..http_utils import EDITHError
from .orchestrator import PushRejected, WorkspacePush


class PushRequest(BaseModel):
    """推送请求。userId 由 BFF 注入。

    branch 留空则用沙箱内仓库的当前分支；remote_branch 留空则与 branch 同名
    （两者可不同，例如本地 master → 远端 main）。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    session_id: str = ""
    workspace_id: str = ""
    branch: str = ""
    remote_branch: str = ""


class HTTP:
    """gitops 对 Web BFF 公开的 HTTP 能力。"""

    def __init__(self, pusher: WorkspacePush) -> None:
        self._pusher = pusher
        self.router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/internal/git/push")
        async def push(body: PushRequest) -> dict:
            user_id = body.user_id.strip()
            session_id = body.session_id.strip()
            workspace_id = body.workspace_id.strip()
            if not user_id or not session_id or not workspace_id:
                raise EDITHError(
                    400, "invalid_request", "userId / sessionId / workspaceId are required"
                )
            try:
                result = await self._pusher.push(
                    user_id, session_id, workspace_id, body.branch, body.remote_branch
                )
            except PushRejected as exc:
                raise EDITHError(exc.status, exc.code, str(exc)) from exc
            return {"ok": True, **result}

        return router
