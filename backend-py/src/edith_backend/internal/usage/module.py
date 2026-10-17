"""usage 模块的组装入口。

对应 Go 版 internal/usage/module.go。
"""

from __future__ import annotations

import aiosqlite

from .reader import Reader
from .recorder import Recorder
from .store import Store


class Module:
    """用量模块对外提供的能力集合。"""

    def __init__(self, recorder: Recorder, reader: Reader) -> None:
        self.recorder = recorder
        self.reader = reader


async def new_module(db: aiosqlite.Connection) -> Module:
    """创建用量模块，并在传入的数据库中创建自己的表。"""
    if db is None:
        raise ValueError("usage database is required")
    store = Store(db)
    await store.create_schema()
    return Module(Recorder(store), Reader(store))
