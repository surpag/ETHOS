"""MCP 服务配置的 HTTP 管理接口。

对应 Go 版 internal/userconfig/mcp_http.go 与 routes.go 的 MCP 路由。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..http_utils import EDITHError
from .mcp import (
    MCP,
    InvalidMCPServer,
    MCPHeaderState,
    MCPServer,
    MCPServerInput,
    MCPServerListResponse,
    MCPServerNotFound,
    MCPServerRequest,
    MCPServerResponse,
)


def _server_response(server: MCPServer) -> MCPServerResponse:
    return MCPServerResponse(
        id=server.id,
        name=server.name,
        url=server.url,
        transport=server.transport,
        enabled=server.enabled,
        headers=[MCPHeaderState(name=h.name, has_value=bool(h.value)) for h in server.headers],
    )


def build_mcp_router(mcp: MCP) -> APIRouter:
    router = APIRouter()

    @router.get("/internal/mcp-servers", response_model=MCPServerListResponse)
    async def list_servers(userId: str = "") -> MCPServerListResponse:
        if not userId.strip():
            raise EDITHError(400, "invalid_request", "userId is required")
        servers = await mcp.list(userId)
        return MCPServerListResponse(servers=[_server_response(s) for s in servers])

    @router.post("/internal/mcp-servers", response_model=MCPServerResponse)
    async def create_server(body: MCPServerRequest) -> MCPServerResponse:
        try:
            server = await mcp.create(
                body.user_id,
                MCPServerInput(
                    name=body.name, url=body.url, transport=body.transport,
                    enabled=body.enabled,
                    headers=[(h.name, h.value) for h in body.headers],
                ),
            )
        except InvalidMCPServer as exc:
            raise EDITHError(400, "invalid_mcp_server", str(exc))
        return _server_response(server)

    @router.put("/internal/mcp-servers/{server_id}", response_model=MCPServerResponse)
    async def update_server(server_id: str, body: MCPServerRequest) -> MCPServerResponse:
        try:
            server = await mcp.update(
                body.user_id, server_id,
                MCPServerInput(
                    name=body.name, url=body.url, transport=body.transport,
                    enabled=body.enabled,
                    headers=[(h.name, h.value) for h in body.headers],
                ),
            )
        except InvalidMCPServer as exc:
            raise EDITHError(400, "invalid_mcp_server", str(exc))
        except MCPServerNotFound:
            raise EDITHError(404, "mcp_server_not_found", "MCP server not found")
        return _server_response(server)

    @router.delete("/internal/mcp-servers/{server_id}", status_code=204)
    async def delete_server(server_id: str, userId: str = "") -> None:
        try:
            await mcp.delete(userId, server_id)
        except MCPServerNotFound:
            raise EDITHError(404, "mcp_server_not_found", "MCP server not found")

    return router
