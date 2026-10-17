"""删除传播测试（P1）：让「删除」在双向同步里真正可表达。

背景（真实故障）：Agent 三次删沙箱 `mp/agent-learn` 都返回 "path deleted"，
但用户隔天看「还在」。根因是同步只有 upsert、从不删对侧，形成双向复活死锁：

- 本地删 → 沙箱还有 → 任意写类工具后 B2 回拉（mkdir + write）→ 本地复活
- 沙箱删 → 本地还有 → 任意写类工具前 B3 推送 → 沙箱复活

本文件钉住的设计：维护「两侧共识文件集合」（synced），每次同步只做一次集合差
（synced − 当前集合 = 该侧已消失），不关心用什么命令删的。因此
`sandbox_delete_path` / `run_command rm` / `git checkout` / 用户在 IDE 手删
走的是同一条路径，不需要任何按来源分支的特例。

同时钉住全部误删护栏（这是最贵的错误）：
- 无状态文件 → 只建基线，不传播
- 沙箱重建（sandbox_id 变化）→ B2 不传播（否则新沙箱为空会把本地删光）
- 本地目录为空 → B3 不传播（盘未挂载 / 被清空的保护）
- 单轮删除上限
"""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from e2b import FileType

from edith_backend.internal.sandbox.tools import _prune_empty_parents
from edith_backend.internal.workspace import sync as sync_module
from edith_backend.internal.workspace.sync import (
    load_synced_paths,
    save_synced_paths,
    sync_local_to_sandbox,
    sync_sandbox_to_local,
)

WS_ID = "ws-del"
MOUNT = f"/home/user/workspace/{WS_ID}"
SANDBOX_ID = "sbx-1"


@dataclass
class FakeEntry:
    path: str
    size: int
    type: FileType = FileType.FILE


@dataclass
class FakeFiles:
    """模拟 E2B 文件系统（文件 + 目录，remove 递归）。

    两个关键语义对齐真实实现：
    - `remove` 对目录是**递归删除**的（这正是它能把 `mp/agent-learn` 整棵树删掉、
      也是「空父目录回收必须先确认为空」的原因）；
    - 空目录是独立存在的实体（文件删光后目录壳仍在，列表里名字还在）。
    """

    entries: list[FakeEntry] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    list_error: Exception | None = None
    read_calls: list[str] = field(default_factory=list)
    write_calls: list[str] = field(default_factory=list)
    remove_calls: list[str] = field(default_factory=list)

    def seed(self, rel: str, data: bytes) -> None:
        path = f"{MOUNT}/{rel}"
        self.blobs[path] = data
        self.entries.append(FakeEntry(path=path, size=len(data)))
        parent = posixpath.dirname(path)
        while parent.startswith(MOUNT + "/"):
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)

    def mkdir(self, rel: str) -> None:
        path = f"{MOUNT}/{rel}"
        parent = path
        while parent.startswith(MOUNT + "/"):
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)

    async def list(self, path: str, depth: int = 1) -> list[FakeEntry]:
        if self.list_error is not None:
            raise self.list_error
        if path == MOUNT:
            return list(self.entries)
        return [e for e in self.entries if e.path.startswith(path + "/")]

    async def read(self, path: str, format: str = "text", request_timeout=None):
        self.read_calls.append(path)
        return bytearray(self.blobs.get(path, b""))

    async def write(self, path: str, data) -> None:
        self.write_calls.append(path)
        raw = bytes(data)
        self.blobs[path] = raw
        self.entries.append(FakeEntry(path=path, size=len(raw)))
        self.mkdir(path[len(MOUNT) + 1 :])

    async def remove(self, path: str) -> None:
        self.remove_calls.append(path)
        exists = (
            path in self.blobs
            or path in self.dirs
            or any(e.path == path or e.path.startswith(path + "/") for e in self.entries)
        )
        if not exists:
            raise FileNotFoundError(path)
        for key in [k for k in self.blobs if k == path or k.startswith(path + "/")]:
            del self.blobs[key]
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(path + "/")}
        self.entries = [
            e
            for e in self.entries
            if e.path != path and not e.path.startswith(path + "/")
        ]

    def names(self) -> set[str]:
        return {e.path[len(MOUNT) + 1 :] for e in self.entries}

    def dir_names(self) -> set[str]:
        return {d[len(MOUNT) + 1 :] for d in self.dirs}


class FakeSandbox:
    def __init__(self, files: FakeFiles, sandbox_id: str = SANDBOX_ID) -> None:
        self.files = files
        self.sandbox_id = sandbox_id


def _ws(tmp_path: Path) -> Path:
    """本地工作区根。刻意与状态目录分开——否则 B3 会把状态文件当工作区文件推走。"""
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _consensus(tmp_path: Path, paths, sandbox_id: str | None = SANDBOX_ID) -> None:
    save_synced_paths(WS_ID, paths, sandbox_id, _state(tmp_path))


# ---- B2：沙箱侧删除 → 传播到本地 ------------------------------------------------------


def test_b2_propagates_sandbox_delete_to_local(tmp_path: Path) -> None:
    """沙箱里消失的文件（Agent 删的 / rm -rf 删的）会同步从本地删掉。"""
    ws = _ws(tmp_path)
    (ws / "a.py").write_bytes(b"aa")
    (ws / "b.py").write_bytes(b"bb")
    files = FakeFiles()
    files.seed("a.py", b"aa")  # 沙箱只剩 a.py，b.py 已被删
    _consensus(tmp_path, {"a.py", "b.py"})

    changed = asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert changed == 0
    assert (ws / "a.py").exists()
    assert not (ws / "b.py").exists()  # 关键：不再被回拉复活
    synced, _ = load_synced_paths(WS_ID, _state(tmp_path))
    assert synced == {"a.py"}


def test_b2_skips_propagation_when_sandbox_rebuilt(tmp_path: Path) -> None:
    """沙箱重建（sandbox_id 变化）时新沙箱天然为空——绝不能据此删光本地。"""
    ws = _ws(tmp_path)
    (ws / "a.py").write_bytes(b"aa")
    (ws / "b.py").write_bytes(b"bb")
    files = FakeFiles()  # 新沙箱：空
    _consensus(tmp_path, {"a.py", "b.py"}, sandbox_id="sbx-old")

    asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files, sandbox_id="sbx-new"),
            WS_ID,
            ws,
            state_dir=_state(tmp_path),
        )
    )

    assert (ws / "a.py").exists()
    assert (ws / "b.py").exists()


def test_b2_skips_propagation_without_state(tmp_path: Path) -> None:
    """首次同步（无共识状态）只建基线，不传播删除。"""
    ws = _ws(tmp_path)
    (ws / "keep.py").write_bytes(b"k")
    files = FakeFiles()  # 沙箱空

    asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert (ws / "keep.py").exists()
    # 基线已建立（空沙箱 → 空集合），下次开始才具备传播能力
    synced, sandbox_id = load_synced_paths(WS_ID, _state(tmp_path))
    assert synced == set()
    assert sandbox_id == SANDBOX_ID


def test_b2_reclaims_empty_parent_dirs_only(tmp_path: Path) -> None:
    """删空目录里的最后一个文件时，空父目录一并回收（不留空壳）。"""
    ws = _ws(tmp_path)
    (ws / "pkg" / "sub").mkdir(parents=True)
    (ws / "pkg" / "sub" / "x.py").write_bytes(b"x")
    (ws / "other.py").write_bytes(b"o")  # 锚文件：避免触发「整体消失」护栏
    files = FakeFiles()
    files.seed("other.py", b"o")
    _consensus(tmp_path, {"pkg/sub/x.py", "other.py"})

    asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert not (ws / "pkg" / "sub").exists()
    assert not (ws / "pkg").exists()
    assert ws.is_dir()  # 工作区根绝不被回收


def test_b2_keeps_parent_dir_with_other_files(tmp_path: Path) -> None:
    """父目录里还有其它文件时不得回收。"""
    ws = _ws(tmp_path)
    (ws / "pkg").mkdir()
    (ws / "pkg" / "gone.py").write_bytes(b"g")
    (ws / "pkg" / "stay.py").write_bytes(b"s")
    files = FakeFiles()
    files.seed("pkg/stay.py", b"s")
    _consensus(tmp_path, {"pkg/gone.py", "pkg/stay.py"})

    asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert not (ws / "pkg" / "gone.py").exists()
    assert (ws / "pkg" / "stay.py").exists()
    assert (ws / "pkg").is_dir()


# ---- B3：本地侧删除 → 传播到沙箱 ------------------------------------------------------


def test_b3_propagates_local_delete_to_sandbox(tmp_path: Path) -> None:
    """本地删掉的文件（用户在 IDE 删的）会从沙箱删掉，且不会被重新推回去。"""
    ws = _ws(tmp_path)
    (ws / "a.py").write_bytes(b"aa")  # 本地只剩 a.py
    files = FakeFiles()
    files.seed("a.py", b"aa")
    files.seed("b.py", b"bb")
    _consensus(tmp_path, {"a.py", "b.py"})

    changed = asyncio.run(
        sync_local_to_sandbox(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert changed == 0
    assert f"{MOUNT}/b.py" in files.remove_calls
    assert files.names() == {"a.py"}
    assert files.write_calls == []  # 关键：没有把 b.py 重新推回来
    synced, _ = load_synced_paths(WS_ID, _state(tmp_path))
    assert synced == {"a.py"}


def test_b3_skips_propagation_when_local_dir_empty(tmp_path: Path) -> None:
    """本地被整个清空 → 共识集合整体消失 → 疑似盘未挂载，不自动删沙箱。

    局部删除会正常传播；只有「全部消失」才按可疑处理（见 B2 的同名用例）。
    """
    ws = _ws(tmp_path)
    files = FakeFiles()
    files.seed("important.py", b"data")
    _consensus(tmp_path, {"important.py"})

    asyncio.run(
        sync_local_to_sandbox(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert files.remove_calls == []
    assert files.names() == {"important.py"}


def test_b2_skips_propagation_on_full_wipe(tmp_path: Path) -> None:
    """沙箱整体为空而共识非空 → 疑似沙箱被重置，不删本地（对称护栏）。"""
    ws = _ws(tmp_path)
    (ws / "keep1.py").write_bytes(b"1")
    (ws / "keep2.py").write_bytes(b"2")
    files = FakeFiles()  # list 拿到空树
    _consensus(tmp_path, {"keep1.py", "keep2.py"})

    asyncio.run(
        sync_sandbox_to_local(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert (ws / "keep1.py").exists()
    assert (ws / "keep2.py").exists()


def test_b3_respects_max_deletes_guard(tmp_path: Path, monkeypatch) -> None:
    """单轮删除数超过上限时整轮跳过（视为状态异常，而非真的删了这么多）。"""
    monkeypatch.setattr(sync_module, "SYNC_MAX_DELETES", 2)
    ws = _ws(tmp_path)
    (ws / "only.py").write_bytes(b"o")
    files = FakeFiles()
    files.seed("a.py", b"a")
    files.seed("b.py", b"b")
    _consensus(tmp_path, {"only.py", "a.py", "b.py", "c.py"})  # vanished = 3 > 2

    asyncio.run(
        sync_local_to_sandbox(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    # 护栏只拦删除；正常的新增推送不受影响
    assert files.remove_calls == []
    assert files.names() == {"a.py", "b.py", "only.py"}


def test_b3_propagation_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    """EDITH_SYNC_PROPAGATE_DELETES=false 时退回纯 upsert 旧行为（可回滚）。"""
    monkeypatch.setattr(sync_module, "SYNC_PROPAGATE_DELETES", False)
    ws = _ws(tmp_path)
    (ws / "a.py").write_bytes(b"aa")
    files = FakeFiles()
    files.seed("a.py", b"aa")
    files.seed("b.py", b"bb")
    _consensus(tmp_path, {"a.py", "b.py"})

    asyncio.run(
        sync_local_to_sandbox(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    assert files.remove_calls == []
    assert files.names() == {"a.py", "b.py"}


# ---- 端到端：死锁消除 ----------------------------------------------------------------


def test_delete_is_not_resurrected_across_rounds(tmp_path: Path) -> None:
    """复现原始故障并证明已修复：沙箱删 → B2 删本地 → B3 不再推回沙箱。

    旧实现里这一步会无限循环：B2 只拉不删（本地留着），B3 只推不删（沙箱复活）。
    """
    ws = _ws(tmp_path)
    (ws / "a.py").write_bytes(b"aa")
    (ws / "b.py").write_bytes(b"bb")
    files = FakeFiles()
    files.seed("a.py", b"aa")
    files.seed("b.py", b"bb")
    _consensus(tmp_path, {"a.py", "b.py"})
    sandbox = FakeSandbox(files)
    state = _state(tmp_path)

    # 第 1 轮：Agent 在沙箱删掉 b.py（sandbox_delete_path / rm 都行，这里直接模拟结果）
    asyncio.run(files.remove(f"{MOUNT}/b.py"))
    asyncio.run(sync_sandbox_to_local(sandbox, WS_ID, ws, state_dir=state))
    assert not (ws / "b.py").exists()

    # 第 2 轮：下一次写类工具前的 B3 —— 必须不把 b.py 推回沙箱
    asyncio.run(sync_local_to_sandbox(sandbox, WS_ID, ws, state_dir=state))
    assert "b.py" not in files.names()

    # 第 3 轮：再跑一次 B2，仍不复活
    asyncio.run(sync_sandbox_to_local(sandbox, WS_ID, ws, state_dir=state))
    assert not (ws / "b.py").exists()


def test_first_sync_establishes_consensus(tmp_path: Path) -> None:
    """首次 B3 推送后，共识集合应包含已推送的路径，供后续删除传播使用。"""
    ws = _ws(tmp_path)
    (ws / "x.py").write_bytes(b"x")
    (ws / "y.py").write_bytes(b"y")
    files = FakeFiles()

    asyncio.run(
        sync_local_to_sandbox(
            FakeSandbox(files), WS_ID, ws, state_dir=_state(tmp_path)
        )
    )

    synced, sandbox_id = load_synced_paths(WS_ID, _state(tmp_path))
    assert synced == {"x.py", "y.py"}
    assert sandbox_id == SANDBOX_ID


# ---- 空父目录回收（tools.py） --------------------------------------------------------


def test_prune_empty_parents_stops_at_workspace_root(tmp_path: Path) -> None:
    """删 mp/agent-learn 后，空壳 mp 一并回收，但绝不越过 workspace/{id}。"""
    files = FakeFiles()
    files.seed("mp/agent-learn/x.py", b"x")
    asyncio.run(files.remove(f"{MOUNT}/mp/agent-learn"))

    reclaimed = asyncio.run(
        _prune_empty_parents(FakeSandbox(files), f"{MOUNT}/mp/agent-learn")
    )

    assert reclaimed == [f"workspace/{WS_ID}/mp"]
    assert MOUNT not in files.remove_calls  # 关键：不删工作区根


def test_prune_empty_parents_keeps_non_empty_dir(tmp_path: Path) -> None:
    """父目录还有别的文件时保留，避免误删（remove 对目录是递归的）。"""
    files = FakeFiles()
    files.seed("mp/agent-learn/x.py", b"x")
    files.seed("mp/keep.txt", b"k")
    asyncio.run(files.remove(f"{MOUNT}/mp/agent-learn"))

    reclaimed = asyncio.run(
        _prune_empty_parents(FakeSandbox(files), f"{MOUNT}/mp/agent-learn")
    )

    assert reclaimed == []
    assert "mp/keep.txt" in files.names()
