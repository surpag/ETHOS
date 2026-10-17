"""pytest 共享 fixture。

app 是全局单例，其 lifespan（创建模块 + 注册路由 + 打开数据库）只应运行一次。
因此用 session 级 fixture 让所有测试文件共享同一个 TestClient 和同一个数据库，
避免多个 module fixture 重复触发 lifespan 导致路由重复注册、闭包捕获已关闭的连接。

各测试通过独立 userId 隔离数据。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from edith_backend.main import app


@pytest.fixture(scope="session")
def client(tmp_path_factory) -> TestClient:
    db_path = tmp_path_factory.mktemp("data") / "edith-test.db"
    previous = os.environ.get("EDITH_DB_PATH")
    os.environ["EDITH_DB_PATH"] = str(db_path)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        if previous is None:
            os.environ.pop("EDITH_DB_PATH", None)
        else:
            os.environ["EDITH_DB_PATH"] = previous


@pytest.fixture(autouse=True)
def _isolate_sync_state(tmp_path, monkeypatch):
    """把同步共识状态重定向到临时目录。

    生产默认落在数据库同级的 workspace-sync/；测试若沿用会污染仓库，
    且各用例共用同一 workspace_id 而互相干扰（前一个用例写下的共识集合
    会让后一个用例误判「对侧被删了」）。
    """
    from edith_backend.internal.workspace import sync as sync_module

    state_dir = tmp_path / "_sync_state"
    monkeypatch.setattr(sync_module, "sync_state_dir", lambda: state_dir)
