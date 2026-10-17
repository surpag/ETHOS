"""模型目录模块的组装入口。

对应 Go 版 internal/models/module.go 的 Module 与 New。

`custom` 承载用户在设置页新增的供应商与模型（可为 None：无数据库上下文的
场景，如离线脚本、部分单测）。它只是 Catalog 的叠加层数据源，不改变内置目录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .catalog import Catalog
from .http import HTTP

if TYPE_CHECKING:
    from ..userconfig.providers import Providers
    from .custom import CustomModels


class Module:
    """模型目录模块对外公开的能力集合。"""

    def __init__(self, catalog: Catalog, http: HTTP) -> None:
        self.catalog = catalog
        self.http = http


def new_module(providers: Providers, custom: CustomModels | None = None) -> Module:
    """创建模型目录模块；Providers 供查询可用模型、写入自定义供应商的 Key。"""
    if providers is None:
        raise ValueError("models requires provider credentials")
    catalog = Catalog(custom=custom)
    http = HTTP(catalog, providers, custom)
    return Module(catalog, http)
