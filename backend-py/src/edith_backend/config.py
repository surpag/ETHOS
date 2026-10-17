"""环境变量加载与配置读取。

对应 Go 版 cmd/server/config.go。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    """加载本地开发配置；进程环境变量优先。

    兼容两种布局：backend-py/.env（旧），或仓库根目录 .env（统一配置）。
    在 backend-py 下运行时 ../.env 即根目录；从根目录运行时 .env 即根目录。
    """
    for path in (Path(".env"), Path("../.env")):
        if path.exists():
            _load_dotenv_tolerant(path)


def _load_dotenv_tolerant(path: Path) -> None:
    """按多种编码尝试加载 .env。

    根目录 .env 可能由 Windows 记事本保存为 GBK 编码，
    而 python-dotenv 默认按 UTF-8 解码会崩溃，这里做编码回退。
    """
    for encoding in ("utf-8", "gbk"):
        try:
            load_dotenv(path, encoding=encoding)
            return
        except UnicodeDecodeError:
            continue
    # 两种编码都无法解码时，抛出以暴露问题（对齐 Go 版 log.Fatalf）
    raise RuntimeError(f"无法解码环境变量文件: {path}")


def database_path() -> str:
    """数据库文件路径，对应 Go 版 EDITH_DB_PATH。"""
    return os.getenv("EDITH_DB_PATH", "edith-v2.db")


def runtime_address() -> str:
    """HTTP 监听地址，对应 Go 版 EDITH_ADDR。"""
    return os.getenv("EDITH_ADDR", "127.0.0.1:8080")


def sandbox_template() -> str:
    """E2B Sandbox 模板，必填配置（阶段 5 使用）。"""
    template = os.getenv("EDITH_E2B_TEMPLATE", "")
    if not template:
        raise RuntimeError("EDITH_E2B_TEMPLATE 是必填配置")
    return template
