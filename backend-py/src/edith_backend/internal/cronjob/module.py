"""cronjob 模块的组装入口。

对应 Go 版 internal/cronjob/module.go。
"""

from __future__ import annotations

import aiosqlite

from ..userconfig.settings import Settings
from .http import HTTP
from .scheduler import Scheduler
from .store import Store
from .tools import CronToolSet


class Module:
    """定时任务模块对外提供的能力集合。"""

    def __init__(self, store: Store, http: HTTP, tools: CronToolSet) -> None:
        self.store = store
        self.http = http
        self.tools = tools

    def new_scheduler(self, runner) -> Scheduler:
        return Scheduler(self.store, runner)


async def new_module(db: aiosqlite.Connection, settings: Settings) -> Module:
    """创建定时任务模块。"""
    store = Store(db, settings)
    return Module(store, HTTP(store, settings), CronToolSet(store))
