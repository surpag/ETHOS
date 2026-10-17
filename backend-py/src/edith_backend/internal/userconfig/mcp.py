"""MCP 服务配置的读写与安全校验。

对应 Go 版 internal/userconfig/mcp.go + mcp_validation.go。
MCP 运行时工具连接（OpenTools）待后续接入 mcp SDK。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .secret import deobfuscate, obfuscate
from .store import ensure_user


class InvalidMCPServer(Exception):
    pass


class MCPServerNotFound(Exception):
    pass


@dataclass
class MCPHeader:
    name: str
    value: str = ""


@dataclass
class MCPServerInput:
    name: str = ""
    url: str = ""
    transport: str = ""
    enabled: bool = True
    headers: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class MCPServer:
    id: str
    name: str
    url: str
    transport: str
    enabled: bool
    headers: list[MCPHeader] = field(default_factory=list)


class MCPHeaderInput(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str = ""
    value: str | None = None


class MCPServerRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    user_id: str = ""
    name: str = ""
    url: str = ""
    transport: str = ""
    enabled: bool = True
    headers: list[MCPHeaderInput] = Field(default_factory=list)


class MCPHeaderState(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    has_value: bool


class MCPServerResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: str
    url: str
    transport: str
    enabled: bool
    headers: list[MCPHeaderState]


class MCPServerListResponse(BaseModel):
    servers: list[MCPServerResponse]


_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _validate_input(input_: MCPServerInput, require_header_values: bool) -> MCPServerInput:
    input_.name = input_.name.strip()
    input_.url = input_.url.strip()
    input_.transport = input_.transport.strip()
    if not input_.name:
        raise InvalidMCPServer("name is required")
    if input_.transport not in ("streamable", "sse"):
        raise InvalidMCPServer("unsupported transport")
    parsed = urlparse(input_.url)
    if not parsed.hostname or parsed.scheme not in ("http", "https"):
        raise InvalidMCPServer("URL must be HTTP or HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise InvalidMCPServer("local MCP URLs are not allowed")
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
            raise InvalidMCPServer("local MCP URLs are not allowed")
    except ValueError:
        pass  # 不是 IP，跳过
    seen: set[str] = set()
    normalized: list[tuple[str, str | None]] = []
    for name, value in input_.headers:
        name = name.strip()
        if not _HEADER_NAME_PATTERN.match(name):
            raise InvalidMCPServer("invalid header name")
        key = name.lower()
        if key in seen:
            raise InvalidMCPServer("duplicate header name")
        seen.add(key)
        if value is not None:
            value = value.strip() or None
        if require_header_values and value is None:
            raise InvalidMCPServer("new header requires a value")
        normalized.append((name, value))
    input_.headers = normalized
    return input_


class MCP:
    """提供 MCP 服务配置的读写能力。"""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def list(self, user_id: str, enabled_only: bool = False) -> list[MCPServer]:
        await ensure_user(self._db, user_id)
        query = "SELECT server_id, name, url, transport, enabled FROM user_mcp_servers WHERE user_id = ?"
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY created_at, server_id"
        cursor = await self._db.execute(query, (user_id.strip(),))
        rows = await cursor.fetchall()
        await cursor.close()
        servers = [
            MCPServer(id=r["server_id"], name=r["name"], url=r["url"],
                      transport=r["transport"], enabled=bool(r["enabled"]))
            for r in rows
        ]
        for server in servers:
            server.headers = await self._load_headers(server.id)
        return servers

    async def create(self, user_id: str, input_: MCPServerInput) -> MCPServer:
        input_ = _validate_input(input_, True)
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id is required")
        await ensure_user(self._db, user_id)
        server = MCPServer(id=uuid4().hex, name=input_.name, url=input_.url,
                           transport=input_.transport, enabled=input_.enabled)
        await self._db.execute(
            "INSERT INTO user_mcp_servers (server_id, user_id, name, url, transport, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (server.id, user_id, server.name, server.url, server.transport, int(server.enabled)),
        )
        for name, value in input_.headers:
            if value is not None:
                await self._db.execute(
                    "INSERT INTO user_mcp_headers (server_id, header_name, header_value) VALUES (?, ?, ?)",
                    (server.id, name, obfuscate(value)),
                )
        await self._db.commit()
        server.headers = [MCPHeader(name=n, value=v or "") for n, v in input_.headers if v is not None]
        return server

    async def update(self, user_id: str, server_id: str, input_: MCPServerInput) -> MCPServer:
        input_ = _validate_input(input_, False)
        cursor = await self._db.execute(
            "UPDATE user_mcp_servers SET name = ?, url = ?, transport = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND server_id = ?",
            (input_.name, input_.url, input_.transport, int(input_.enabled), user_id.strip(), server_id.strip()),
        )
        if cursor.rowcount == 0:
            raise MCPServerNotFound("MCP server not found")
        # 替换 headers（只更新提交的，未提交的保留）
        for name, value in input_.headers:
            if value is None:
                continue  # 不覆盖
            await self._db.execute(
                "INSERT INTO user_mcp_headers (server_id, header_name, header_value) VALUES (?, ?, ?) "
                "ON CONFLICT(server_id, header_name) DO UPDATE SET header_value = excluded.header_value",
                (server_id, name, obfuscate(value)),
            )
        await self._db.commit()
        return await self._load_one(user_id, server_id)

    async def delete(self, user_id: str, server_id: str) -> None:
        cursor = await self._db.execute(
            "DELETE FROM user_mcp_servers WHERE user_id = ? AND server_id = ?",
            (user_id.strip(), server_id.strip()),
        )
        if cursor.rowcount == 0:
            raise MCPServerNotFound("MCP server not found")
        await self._db.execute("DELETE FROM user_mcp_headers WHERE server_id = ?", (server_id.strip(),))
        await self._db.commit()

    async def _load_headers(self, server_id: str) -> list[MCPHeader]:
        cursor = await self._db.execute(
            "SELECT header_name, header_value FROM user_mcp_headers WHERE server_id = ? ORDER BY header_name",
            (server_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            MCPHeader(name=r["header_name"], value=deobfuscate(r["header_value"]))
            for r in rows
        ]

    async def _load_one(self, user_id: str, server_id: str) -> MCPServer:
        cursor = await self._db.execute(
            "SELECT server_id, name, url, transport, enabled FROM user_mcp_servers WHERE user_id = ? AND server_id = ?",
            (user_id.strip(), server_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise MCPServerNotFound("MCP server not found")
        server = MCPServer(id=row["server_id"], name=row["name"], url=row["url"],
                           transport=row["transport"], enabled=bool(row["enabled"]))
        server.headers = await self._load_headers(server_id)
        return server
