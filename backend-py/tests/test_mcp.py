"""GitHub Copilot MCP 手动连通性探针（不是 pytest 用例）。

用法：

    export MCP_URL="https://api.githubcopilot.com/mcp/"
    export MCP_TOKEN="ghp_xxx"          # 只放本地环境变量，绝不写进代码
    python tests/test_mcp.py

注意：token 一律从环境变量读。把 PAT 硬编码进源码会被 GitHub Push Protection
拦下（GH013），且一旦外泄就必须吊销重签。
"""

import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("MCP_URL", "https://api.githubcopilot.com/mcp/")
TOKEN = os.environ.get("MCP_TOKEN", "")
TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable")  # 或 "sse"


async def main() -> int:
    if not TOKEN:
        print("缺少 MCP_TOKEN 环境变量（GitHub PAT），请先 export 再运行。", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Bearer {TOKEN}"}
    if TRANSPORT == "streamable":
        async with httpx.AsyncClient(headers=headers) as http:
            async with streamable_http_client(URL, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    print(f"连接成功，共 {len(result.tools)} 个工具：")
                    for tool in result.tools:
                        print(f"  - {tool.name}: {tool.description}")
    else:
        from mcp.client.sse import sse_client

        async with sse_client(URL, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                print(f"连接成功，共 {len(result.tools)} 个工具：")
                for tool in result.tools:
                    print(f"  - {tool.name}: {tool.description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
