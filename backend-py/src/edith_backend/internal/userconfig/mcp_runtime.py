"""MCP 运行时：连接用户启用的 MCP 服务并暴露其工具。

对应 Go 版 internal/userconfig/mcp_runtime.go。
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .mcp import MCP, MCPServer

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^a-z0-9]+")


def _toolset_name(server: MCPServer) -> str:
    """生成该 MCP 服务的工具集名称，用于给工具加前缀避免冲突。"""
    name = server.name.lower()
    prefix = _UNSAFE_CHARS.sub("_", name).strip("_") or "mcp"
    short_id = server.id.replace("-", "")[:8]
    return f"{prefix}_{short_id}"


class _ServerConnection:
    """一次 AgentRun 内对一个 MCP 服务的连接与工具映射。"""

    def __init__(self, session: ClientSession, transport_cm) -> None:
        self.session = session
        self._transport_cm = transport_cm
        self.tools: dict[str, str] = {}  # prefixed_name -> original_name
        self.definitions: list[dict] = []  # OpenAI function calling 格式

    async def close(self) -> None:
        try:
            await self.session.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001, S110 - 关闭失败不影响其他连接
            pass
        try:
            await self._transport_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001, S110
            pass


class MCPToolContext:
    """一次 AgentRun 内所有已连接 MCP 服务的工具集合。"""

    def __init__(self) -> None:
        self._connections: list[_ServerConnection] = []

    @property
    def has_tools(self) -> bool:
        return any(connection.definitions for connection in self._connections)

    def has(self, name: str) -> bool:
        return any(name in connection.tools for connection in self._connections)

    def tool_definitions(self) -> list[dict]:
        return [d for connection in self._connections for d in connection.definitions]

    async def execute(self, name: str, arguments: dict) -> str:
        for connection in self._connections:
            original = connection.tools.get(name)
            if original is None:
                continue
            result = await connection.session.call_tool(original, arguments)
            text = _call_result_text(result)
            if getattr(result, "is_error", False):
                return json.dumps({"error": text}, ensure_ascii=False)
            return text
        return json.dumps({"error": f"unknown MCP tool: {name}"}, ensure_ascii=False)

    async def close(self) -> None:
        for connection in self._connections:
            await connection.close()
        self._connections.clear()


async def _connect_server(server: MCPServer, toolset_name: str) -> _ServerConnection:
    """连接一个 MCP 服务并列出工具，注册到连接对象。"""
    headers = {h.name: h.value for h in server.headers}

    transport_cm = None
    http_client = None
    session = None

    try:
        if server.transport == "sse":
            transport_cm = sse_client(server.url, headers=headers or None)
            streams = await transport_cm.__aenter__()
            read_stream, write_stream = streams
        else:  # streamable
            http_client = httpx.AsyncClient(headers=headers or None)
            transport_cm = streamable_http_client(server.url, http_client=http_client)
            streams = await transport_cm.__aenter__()
            read_stream, write_stream = streams[0], streams[1]

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()

        await session.initialize()
        tools_result = await session.list_tools()
    except Exception:
        # 失败路径：按 session → transport → http_client 顺序清理，每一步独立兜底，
        # 避免前一步抛异常导致后续资源泄漏（async generator 泄漏会在 GC 时跨 task 报错）。
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001, S110 - 清理失败不影响后续资源释放
                pass
        if transport_cm is not None:
            try:
                await transport_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001, S110
                pass
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:  # noqa: BLE001, S110
                pass
        raise

    connection = _ServerConnection(session, transport_cm)
    logger.info(
        "mcp connected server=%s transport=%s tools=%d",
        server.name,
        server.transport,
        len(tools_result.tools),
    )
    for tool in tools_result.tools:
        prefixed = f"{toolset_name}__{tool.name}"
        connection.tools[prefixed] = tool.name
        connection.definitions.append(
            {
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": tool.description or tool.title or tool.name,
                    "parameters": tool.input_schema or {},
                },
            }
        )

    return connection


def _call_result_text(result) -> str:
    """提取 CallToolResult 的文本内容。"""
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


class MCPRuntime:
    """连接用户启用的 MCP 服务，返回本次运行的工具上下文。"""

    def __init__(self, mcp: MCP) -> None:
        self._mcp = mcp

    async def open_tools(self, user_id: str) -> MCPToolContext:
        context = MCPToolContext()
        servers = await self._mcp.list(user_id, enabled_only=True)
        for server in servers:
            try:
                connection = await _connect_server(server, _toolset_name(server))
            except Exception as exc:  # noqa: BLE001 - 单个服务连接失败跳过并记录
                logger.warning("open MCP server %s: %s", server.name, exc)
                continue
            context._connections.append(connection)
        return context
