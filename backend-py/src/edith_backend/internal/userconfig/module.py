"""用户配置模块的组装入口。

对应 Go 版 internal/userconfig/module.go 的 Module 与 New。
"""

from __future__ import annotations

import aiosqlite

from .bindings import Bindings
from .gitcredentials import GitCredentials
from .http import HTTP
from .mcp import MCP
from .providers import Providers
from .schema import create_schema
from .settings import Settings


class Module:
    """用户配置模块对外公开的能力集合。"""

    def __init__(
        self,
        settings: Settings,
        providers: Providers,
        bindings: Bindings,
        mcp: MCP,
        http: HTTP,
        git_credentials: GitCredentials,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.mcp = mcp
        self.bindings = bindings
        self.http = http
        self.git_credentials = git_credentials


async def new_module(db: aiosqlite.Connection, default_model_id: str) -> Module:
    """在调用方提供的数据库上创建用户配置模块。

    对应 Go 版 userconfig.New()：先建表，再创建各子能力并组装 HTTP。
    """
    if db is None:
        raise ValueError("userconfig requires a database")
    await create_schema(db)

    settings = Settings(db)
    providers = Providers(db)
    bindings = Bindings(db)
    mcp = MCP(db)
    git_credentials = GitCredentials(db)
    http = HTTP(settings, providers, git_credentials, default_model_id)

    return Module(settings, providers, bindings, mcp, http, git_credentials)
