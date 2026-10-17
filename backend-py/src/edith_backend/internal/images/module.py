"""images 模块的组装入口。

对应 Go 版 internal/images/module.go。
"""

from __future__ import annotations

import aiosqlite

from .agent_input import AgentInput
from .cos import COSConfig
from .http import HTTP
from .service import Service
from .store import Store, create_schema


class Module:
    """图片模块对外提供的能力集合。"""

    def __init__(self, agent_input: AgentInput, http: HTTP) -> None:
        self.agent_input = agent_input
        self.http = http


async def new_module(db: aiosqlite.Connection, config: COSConfig) -> Module:
    """创建图片模块；数据库由调用方持有，COS 客户端由模块持有。"""
    if db is None:
        raise ValueError("image database is required")
    await create_schema(db)
    store = Store(db)
    service = Service.build(store, config)
    return Module(AgentInput(service), HTTP(service))
