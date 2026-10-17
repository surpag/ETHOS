"""阶段 10 验收测试：工具错误语义分层、熔断、断点续传（纯逻辑，不调外部 API）。

覆盖三块：
1. 错误语义分层（业务错误 command_error / 系统故障 tool_failure）
2. 熔断异常契约
3. 断点续传（task_status / checkpoint / 已读进度 / end_reason）
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiosqlite

from edith_backend.internal.agentrun.execution import (
    ToolCircuitBreakerError,
    _digest,
    _estimate_tokens,
    _parse_result_retryable,
    _read_progress,
    _trim_tool_results,
)
from edith_backend.internal.agentstream.types import Event
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.tools import (
    COMMAND_ERROR,
    TOOL_FAILURE,
    command_stderr_hint,
    tool_error_result,
)

# ---- 一、错误语义分层 ----


def test_parse_result_retryable() -> None:
    assert _parse_result_retryable(json.dumps({"retryable": False})) is False
    assert _parse_result_retryable(json.dumps({"retryable": True})) is True
    assert _parse_result_retryable(json.dumps({"exitCode": 0})) is None
    assert _parse_result_retryable("not json") is None


def test_tool_error_result_business_vs_system() -> None:
    # 业务错误（路径/权限）→ command_error + retryable=True + 带 suggestion
    d = json.loads(tool_error_result("sandbox_read_file", FileNotFoundError("no file")))
    assert d["kind"] == COMMAND_ERROR
    assert d["retryable"] is True
    assert d["suggestion"]
    # 系统故障（SDK 异常/超时）→ tool_failure + retryable=False
    d = json.loads(tool_error_result("sandbox_run_command", TypeError("bad arg")))
    assert d["kind"] == TOOL_FAILURE
    assert d["retryable"] is False


def test_command_stderr_hint_detects_pseudo_success() -> None:
    # exit_code=0 但 stderr 有业务错误（伪成功）
    assert command_stderr_hint("cat: x.py: No such file or directory")
    assert command_stderr_hint("bash: foo: command not found")
    assert command_stderr_hint("Permission denied")
    # 正常输出无命中
    assert command_stderr_hint("") is None
    assert command_stderr_hint("3 files changed, 2 insertions") is None


# ---- 二、熔断 ----


def test_circuit_breaker_error_contract() -> None:
    exc = ToolCircuitBreakerError("sandbox_run_command", 3, "boom")
    assert exc.tool_name == "sandbox_run_command"
    assert exc.failures == 3
    assert exc.last_error == "boom"
    assert "熔断" in str(exc)


# ---- 三、断点续传 ----


def test_digest() -> None:
    assert _digest("short") == "short"
    long = _digest("x" * 500)
    assert "sha256:" in long
    assert "len=500" in long


def test_read_progress() -> None:
    result = json.dumps(
        {"path": "a.py", "startLine": 1, "endLine": 90, "totalLines": 187}
    )
    d = json.loads(_read_progress("sandbox_read_file", result))
    assert d["path"] == "a.py"
    assert d["totalLines"] == 187
    assert d["endLine"] == 90
    # 非 read 工具 / 非 JSON → 空串
    assert _read_progress("sandbox_run_command", result) == ""
    assert _read_progress("sandbox_read_file", "not json") == ""


def test_event_end_reason_serialization() -> None:
    ev = Event(type="run.completed", request_id="r1", end_reason="rounds_exhausted")
    dumped = json.loads(ev.model_dump_json(exclude_none=True, by_alias=True))
    # camelCase 序列化，供前端读取
    assert dumped["endReason"] == "rounds_exhausted"


async def _fresh_store() -> tuple[aiosqlite.Connection, SessionStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, SessionStore(db)


def test_task_status_roundtrip() -> None:
    async def run() -> None:
        db, store = await _fresh_store()
        try:
            u, s = "u1", "s1"
            # 未开始 → None（无需恢复）
            assert await store.get_task_status(u, s) is None
            await store.set_task_status(u, s, "in_progress")
            assert await store.get_task_status(u, s) == "in_progress"
            await store.set_task_status(u, s, "canceled")
            assert await store.get_task_status(u, s) == "canceled"
        finally:
            await db.close()

    asyncio.run(run())


def test_checkpoint_wal_and_read_progress() -> None:
    async def run() -> None:
        db, store = await _fresh_store()
        try:
            u, s = "u1", "s1"
            assert await store.load_latest_checkpoint(u, s) is None
            p = json.dumps(
                {"path": "a.py", "startLine": 1, "endLine": 90, "totalLines": 187}
            )
            await store.append_checkpoint(u, s, 1, "sandbox_read_file", "a", "ok", p)
            await store.append_checkpoint(u, s, 2, "sandbox_run_command", "ls", "x", "")
            ck = await store.load_latest_checkpoint(u, s)
            assert ck is not None
            assert ck["step"] == 2
            assert ck["tool"] == "sandbox_run_command"
            # 只有 read_file 的进度被聚合
            reads = await store.load_read_progress(u, s)
            assert len(reads) == 1
            assert reads[0]["path"] == "a.py"
            # 清理
            await store.clear_checkpoints(u, s)
            assert await store.load_latest_checkpoint(u, s) is None
        finally:
            await db.close()

    asyncio.run(run())


# ---- 四、上下文治理（C1 裁剪 / C2 预算估算）----


def test_estimate_tokens_cjk_aware() -> None:
    # 纯英文：约 len/4
    en = _estimate_tokens([{"role": "tool", "content": "a" * 400}])
    assert en == 100
    # 纯中文：约 0.7/字，比英文同字符数高
    zh = _estimate_tokens([{"role": "tool", "content": "中" * 400}])
    assert zh == 280
    # 空内容 / 非字符串不炸
    assert _estimate_tokens([{"role": "tool", "content": ""}]) == 0
    assert _estimate_tokens([{"role": "tool", "content": None}]) == 0


def test_trim_tool_results_keeps_recent() -> None:
    messages = [{"role": "tool", "content": "x" * 5000} for _ in range(8)]
    _trim_tool_results(messages, keep_recent=3, limit=100)
    # 最近 3 条保留全文
    assert len(messages[7]["content"]) == 5000
    assert len(messages[6]["content"]) == 5000
    assert len(messages[5]["content"]) == 5000
    # 较早的被截断，且带标记
    assert len(messages[0]["content"]) < 5000
    assert "已截断" in messages[0]["content"]
    # keep_recent=0 → 全部截断
    all_trim = [{"role": "tool", "content": "z" * 5000} for _ in range(3)]
    _trim_tool_results(all_trim, keep_recent=0, limit=10)
    assert all("已截断" in m["content"] for m in all_trim)
    # 非 tool 消息不动
    mixed = [{"role": "user", "content": "y" * 5000}]
    _trim_tool_results(mixed, keep_recent=0, limit=10)
    assert len(mixed[0]["content"]) == 5000


# ---- 五、读取效率（B3 大文件按行读 / B4 保首保尾）----


def test_read_file_slice_small_vs_large() -> None:
    from edith_backend.internal.sandbox.tools import (
        _LARGE_FILE_BYTES,
        _read_file_slice,
    )

    class FakeFiles:
        def __init__(self, size: int) -> None:
            self._size = size

        async def get_info(self, path: str):
            return SimpleNamespace(size=self._size)

        async def read(self, path: str) -> str:
            return "small content\nline2"

    class FakeCommands:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, cmd: str):
            self.calls.append(cmd)
            if cmd.startswith("wc"):
                return SimpleNamespace(stdout="1000\n")
            return SimpleNamespace(
                stdout="".join(f"L{i}\n" for i in range(11, 21))
            )

    async def run() -> None:
        # 小文件：直读切片，不走命令
        cmds = FakeCommands()
        sb = SimpleNamespace(files=FakeFiles(100), commands=cmds)
        lines, total, end = await _read_file_slice(sb, "/x.py", 1, 200)
        assert total == 2
        assert lines == ["small content", "line2"]
        assert cmds.calls == []

        # 大文件：sed 按行读 + wc 数行
        cmds = FakeCommands()
        sb = SimpleNamespace(files=FakeFiles(_LARGE_FILE_BYTES + 1), commands=cmds)
        lines, total, end = await _read_file_slice(sb, "/big.log", 11, 10)
        assert total == 1000
        assert len(lines) == 10
        assert end == 20
        assert any("wc -l" in c for c in cmds.calls)
        assert any("sed -n" in c for c in cmds.calls)

    asyncio.run(run())


def test_shorten_head_and_tail() -> None:
    from edith_backend.internal.sandbox.tools import _shorten

    assert _shorten("ok") == ("ok", False)
    text = "A" * 100 + "B" * 100000 + "C" * 100
    out, truncated = _shorten(text)
    assert truncated is True
    assert out.startswith("A" * 100)  # 保首
    assert out.endswith("C" * 100)  # 保尾
    assert "中间省略" in out


def test_workspace_path_authorization() -> None:
    """D1：路径校验必须同时是授权校验——一个 session 只能访问自己的工作区。"""
    import pytest

    from edith_backend.internal.sandbox.workspace import workspace_path

    # 跨工作区 → PermissionError
    with pytest.raises(PermissionError):
        workspace_path("workspace/OTHER/x.py", allow_root=True, current_workspace_id="MINE")
    # 本工作区 / 共享目录 → 放行
    assert workspace_path(
        "workspace/MINE/x.py", True, "MINE"
    ).endswith("/workspace/MINE/x.py")
    assert workspace_path("uploads/a.txt", True, "MINE").endswith("/uploads/a.txt")
    # 未传 current_workspace_id 时保持旧行为（向后兼容）
    assert workspace_path("workspace/ANY/x.py", True).endswith("/workspace/ANY/x.py")
    # 格式校验不受影响
    for bad in ("/etc/passwd", "workspace", "../x", "etc/x"):
        with pytest.raises(ValueError):
            workspace_path(bad, allow_root=False, current_workspace_id="MINE")


def test_path_fields_covers_all_file_tools() -> None:
    """新增文件类工具若忘记登记 _PATH_FIELDS，这里必须失败（防静默放行）。"""
    from edith_backend.internal.sandbox.tools import (
        _FILE_TOOLS,
        _PATH_FIELDS,
        _audit_digest,
        _scope_violation,
    )

    assert _FILE_TOOLS <= set(_PATH_FIELDS), f"漏登记: {_FILE_TOOLS - set(_PATH_FIELDS)}"
    # 越权检测覆盖 path 与 move 的 from/to
    assert _scope_violation("sandbox_read_file", {"path": "workspace/OTHER/x"}, "MINE")
    assert _scope_violation(
        "sandbox_move_path", {"from": "workspace/MINE/a", "to": "workspace/OTHER/b"}, "MINE"
    )
    assert not _scope_violation("sandbox_read_file", {"path": "workspace/MINE/x"}, "MINE")
    # 审计摘要不得包含文件正文
    digest = _audit_digest({"path": "a.py", "content": "x" * 5000})
    assert "<5000 chars>" in digest
    assert "xxxx" not in digest


def test_workspace_lanes_mutual_exclusion() -> None:
    """乙方案：同一工作区同一时刻只允许一个会话；同会话可重入；释放后他人可用。"""
    from edith_backend.internal.agentrun.workspace_lanes import WorkspaceLanes

    lanes = WorkspaceLanes()
    assert lanes.acquire("u1", "w1", "s1") is True
    # 其他会话被拒
    assert lanes.acquire("u1", "w1", "s2") is False
    assert lanes.holder("u1", "w1") == "s1"
    # 同会话可重入（幂等）
    assert lanes.acquire("u1", "w1", "s1") is True
    # 释放后他人可用
    lanes.release("u1", "w1", "s1")
    assert lanes.holder("u1", "w1") is None
    assert lanes.acquire("u1", "w1", "s2") is True
    # 释放别人的租约是 no-op
    lanes.release("u1", "w1", "s1")
    assert lanes.holder("u1", "w1") == "s2"
    # 不同工作区互不影响
    assert lanes.acquire("u1", "w2", "s1") is True


def test_allowed_domains_from_env(monkeypatch: object) -> None:
    """出口白名单来源：环境变量兜底，逗号/分号/空白分隔，去空。"""

    from edith_backend.internal.sandbox.service import allowed_domains_from_env

    monkeypatch.setenv("EDITH_SANDBOX_ALLOWED_DOMAINS", "pypi.org, files.pythonhosted.org; github.com")
    assert allowed_domains_from_env() == [
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
    ]
    monkeypatch.setenv("EDITH_SANDBOX_ALLOWED_DOMAINS", "  ")
    assert allowed_domains_from_env() == []


def test_sensitive_path_violation() -> None:
    """P2-6：文件工具对 .git/.env/私钥/凭据文件一律拒绝；普通文件与 .pub 放行。"""

    from edith_backend.internal.sandbox.tools import _sensitive_path_violation

    # 命中敏感段
    for path in (".git", ".git/config", "workspace/w1/.git", "app/.env",
                 "app/.env.local", ".ssh/id_rsa", "credentials.json",
                 "workspace/w1/id_ed25519"):
        assert _sensitive_path_violation("sandbox_read_file", {"path": path}), path
    # move 的 from/to 都覆盖
    assert _sensitive_path_violation(
        "sandbox_move_path", {"from": "a", "to": "b/.env"}
    ) == ".env"
    # 放行：普通文件、公钥、含 git 字样的正常目录名
    for path in ("docs/report.md", "id_rsa.pub", "my-git-project/main.py",
                 "workspace/w1/env.py", "credentials.go"):
        assert _sensitive_path_violation("sandbox_read_file", {"path": path}) == "", path


def test_run_command_rejects_git() -> None:
    """P2-5：run_command 不再直接执行 git，引导模型走 git 工具（防白名单绕过）。"""

    import asyncio
    from types import SimpleNamespace

    from edith_backend.internal.sandbox.tools import SandboxToolSet

    class FakeCommands:
        def __init__(self) -> None:
            self.called: list[str] = []

        async def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
            self.called.append(cmd)
            return SimpleNamespace(exit_code=0, stdout="", stderr="", error=None)

    async def go() -> None:
        ts = SandboxToolSet(None)
        cmds = FakeCommands()
        sandbox = SimpleNamespace(commands=cmds)
        for cmd in ("git", "git status", "/usr/bin/git"):
            r = await ts._op_sandbox_run_command(sandbox, {"command": cmd})
            assert r["kind"] == "command_error" and r["retryable"] is True, cmd
            assert "git 工具" in r["suggestion"], r
        # git 全部被拦截，未到达执行层
        assert cmds.called == [], cmds.called
        # 非 git 命令正常执行
        await ts._op_sandbox_run_command(sandbox, {"command": "echo", "args": ["hi"]})
        assert cmds.called == ["echo hi"], cmds.called

    asyncio.run(go())


def test_e2b_backend_ensure_volume_returns_name(monkeypatch: object) -> None:
    """M1.5：E2BVolumeBackend.ensure_volume 懒创建并返回 volume_name 供沙箱挂载。"""

    import asyncio
    from types import SimpleNamespace

    import edith_backend.internal.workspace.volume_backend as vb
    from edith_backend.internal.workspace.volume_backend import E2BVolumeBackend

    class FakeVolume:
        def __init__(self, name: str) -> None:
            self.name = name
            self.volume_id = "vid-1"
            self.token = "tok"

    created: list[str] = []

    async def fake_create(**kwargs: object) -> FakeVolume:
        created.append(kwargs["name"])
        return FakeVolume(kwargs["name"])

    monkeypatch.setattr(vb, "AsyncVolume", SimpleNamespace(create=fake_create))

    async def fake_load(user_id: str, workspace_id: str) -> None:
        return None

    async def fake_save(*args: object, **kwargs: object) -> None:
        return None

    store = SimpleNamespace(load_volume=fake_load, save_volume=fake_save)
    backend = E2BVolumeBackend(store)  # type: ignore[arg-type]

    name = asyncio.run(backend.ensure_volume("u1", "w1"))
    assert name == "ws_w1_u1"
    assert created == ["ws_w1_u1"]


def test_e2b_backend_does_not_rebuild_on_connect_failure(monkeypatch: object) -> None:
    """M1.5 隐患修复：已建卷 connect 失败时上抛，绝不静默重建（重建会丢数据）。"""

    import asyncio
    from types import SimpleNamespace

    import edith_backend.internal.workspace.volume_backend as vb
    from edith_backend.internal.workspace.volume_backend import E2BVolumeBackend

    async def fake_connect(volume_id: str) -> None:
        raise ConnectionError("network blip")

    async def fake_create(**kwargs: object) -> None:
        raise AssertionError("connect 失败后不应重建卷")

    async def fake_save(*args: object, **kwargs: object) -> None:
        raise AssertionError("connect 失败后不应覆盖 DB 绑定")

    monkeypatch.setattr(
        vb,
        "AsyncVolume",
        SimpleNamespace(connect=fake_connect, create=fake_create),
    )

    async def fake_load(user_id: str, workspace_id: str) -> tuple[str, str, str]:
        return ("vid-1", "ws_abc_user1", "tok")

    store = SimpleNamespace(load_volume=fake_load, save_volume=fake_save)
    backend = E2BVolumeBackend(store)  # type: ignore[arg-type]

    try:
        asyncio.run(backend.ensure_volume("u1", "w1"))
    except ConnectionError:
        return  # 预期：上抛而非静默重建
    raise AssertionError("connect 失败应上抛 ConnectionError")


def test_git_credentials_roundtrip() -> None:
    """M3a：Git 凭据存取——混淆落盘（非明文）、可覆盖、可删除、拒绝非法输入。"""

    import asyncio

    import aiosqlite
    import pytest

    from edith_backend.internal.userconfig.gitcredentials import GitCredentials
    from edith_backend.internal.userconfig.schema import create_schema

    async def go() -> None:
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await create_schema(db)
        creds = GitCredentials(db)

        assert await creds.list_connected("u1") == []
        await creds.save("u1", "github", "ghp_secret123")
        assert await creds.list_connected("u1") == ["github"]
        assert await creds.load_token("u1", "github") == "ghp_secret123"

        # 落盘必须是混淆值，不含明文
        cursor = await db.execute("SELECT token FROM user_git_credentials")
        row = await cursor.fetchone()
        assert row["token"].startswith("enc:v1:")
        assert "ghp_secret123" not in row["token"]

        # 覆盖与隔离（不同用户互不可见）
        await creds.save("u1", "github", "ghp_new")
        assert await creds.load_token("u1", "github") == "ghp_new"
        assert await creds.load_token("u2", "github") is None

        # 删除
        assert await creds.delete("u1", "github") is True
        assert await creds.list_connected("u1") == []
        assert await creds.load_token("u1", "github") is None

        # 非法输入
        with pytest.raises(ValueError):
            await creds.save("u1", "gitlab", "x")  # 不支持的 provider
        with pytest.raises(ValueError):
            await creds.save("u1", "github", "   ")  # 空 token

        await db.close()

    asyncio.run(go())
