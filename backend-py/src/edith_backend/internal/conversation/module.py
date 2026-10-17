"""conversation 模块的组装入口。

对应 Go 版 internal/conversation/module.go。
"""

from __future__ import annotations

from ..session.store import SessionStore
from ..usage.reader import Reader
from .history import History
from .http import HTTP


class Module:
    """会话模块对外提供的能力集合。"""

    def __init__(self, http: HTTP) -> None:
        self.http = http


def new_module(store: SessionStore, usage: Reader) -> Module:
    """创建会话模块；会话存储和用量读取器都由调用方持有。"""
    if store is None:
        raise ValueError("conversation session store is required")
    if usage is None:
        raise ValueError("conversation usage reader is required")
    history = History(store, usage)
    return Module(HTTP(history))
