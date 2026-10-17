"""B2/B3 同步护栏测试（P0）。

背景：`sync_sandbox_to_local` 会在每个写类工具调用后运行，原实现对工作区内每个
文件做 `files.read(format="bytes")` 全量字节比对；配合 E2B SDK 默认的 60s 整请求
硬超时，图片等大文件被反复拉回并逐个超时（N 个文件 ≈ N×60s 阻塞在工具返回前）。

本文件钉住三道护栏 + 熔断 + 显式短超时的行为，防止回归：
- 免下载比对：本地已有且 size 一致 → 不发任何 read
- 体积上限 / 二进制排除：不回拉
- 熔断：连续失败达阈值即中止本轮
- 显式 request_timeout：绝不落到 SDK 默认 60s
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from e2b import FileType

from edith_backend.internal.workspace.sync import (
    is_binary_like,
    is_excluded_from_pull,
    sync_local_to_sandbox,
    sync_sandbox_to_local,
)

WS_ID = "ws-abc"
MOUNT = f"/home/user/workspace/{WS_ID}"


@dataclass
class FakeEntry:
    """`files.list` 返回的最小条目（对齐 e2b EntryInfo 的 path/size/type）。"""

    path: str
    size: int
    type: FileType = FileType.FILE


@dataclass
class FakeFiles:
    """记录 list/read/write 调用的假文件系统，用于断言"是否发起过远端读取"。"""

    entries: list[FakeEntry] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    read_error: Exception | None = None
    list_error: Exception | None = None
    read_calls: list[tuple[str, str, float | None]] = field(default_factory=list)
    write_calls: list[str] = field(default_factory=list)

    async def list(self, path: str, depth: int = 1) -> list[FakeEntry]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.entries)

    async def read(
        self, path: str, format: str = "text", request_timeout: float | None = None
    ):
        self.read_calls.append((path, format, request_timeout))
        if self.read_error is not None:
            raise self.read_error
        return bytearray(self.blobs.get(path, b""))

    async def write(self, path: str, data) -> None:
        self.write_calls.append(path)
        self.blobs[path] = bytes(data)


class FakeSandbox:
    def __init__(self, files: FakeFiles) -> None:
        self.files = files
        self.sandbox_id = "sbx-fake"


def _entry(rel: str, size: int, kind: FileType = FileType.FILE) -> FakeEntry:
    return FakeEntry(path=f"{MOUNT}/{rel}", size=size, type=kind)


# ---- 护栏判定（纯函数） ---------------------------------------------------------------


def test_is_binary_like_matches_media_and_binaries() -> None:
    assert is_binary_like("a/b/photo.png")
    assert is_binary_like("data/uploads/IMG.JPEG")  # 大小写不敏感
    assert is_binary_like("dist/app.exe")
    assert is_binary_like("assets/font.woff2")
    assert not is_binary_like("src/main.py")
    assert not is_binary_like("docs/report.md")
    assert not is_binary_like("Makefile")  # 无后缀
    assert not is_binary_like("pkg.v2/readme")  # 点在目录名上，不算后缀


def test_is_excluded_from_pull_respects_ignore_and_binary(tmp_path: Path) -> None:
    assert is_excluded_from_pull("node_modules/x.js", [])  # 内置忽略
    assert is_excluded_from_pull("data/a.png", [])  # 二进制护栏
    assert is_excluded_from_pull("data/a.bin", ["data/"])  # 自定义 .edithignore
    assert not is_excluded_from_pull("src/main.py", [])


# ---- B2：免下载比对 -------------------------------------------------------------------


def test_b2_skips_read_when_local_size_matches(tmp_path: Path) -> None:
    """本地已有且大小一致的文件不发起任何远端读取（免下载比对）。

    注意这里刻意让远端内容与本地**不同但同长度**：若走「读回来再比内容」的老路，
    会触发一次 read（并可能覆盖本地）。免下载比对只看 size，所以一次 read 都没有。
    这同时也是已知取舍：同长度改写不会被回拉（由 P1 的 mtime 增量同步收口）。
    """
    (tmp_path / "main.py").write_bytes(b"print(1)\n")  # 9 字节
    files = FakeFiles(
        entries=[_entry("main.py", 9)],
        blobs={f"{MOUNT}/main.py": b"print(2)\n"},  # 同样 9 字节，内容不同
    )

    changed = asyncio.run(sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path))

    assert changed == 0
    assert files.read_calls == []  # 关键：一次 read 都没有
    assert (tmp_path / "main.py").read_bytes() == b"print(1)\n"  # 本地未被覆盖


def test_b2_pulls_changed_text_file(tmp_path: Path) -> None:
    """大小不一致的文本文件会被拉回并落盘（run_command 盲区仍被覆盖）。"""
    files = FakeFiles(
        entries=[_entry("gen.py", 12)],
        blobs={f"{MOUNT}/gen.py": b"generated!!\n"},
    )

    changed = asyncio.run(
        sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path)
    )

    assert changed == 1
    assert (tmp_path / "gen.py").read_bytes() == b"generated!!\n"
    assert [c[0] for c in files.read_calls] == [f"{MOUNT}/gen.py"]


# ---- B2：体积上限 + 二进制排除 --------------------------------------------------------


def test_b2_skips_binary_and_oversize_without_reading(tmp_path: Path) -> None:
    """二进制与超限文件直接跳过，不进入读取路径。"""
    files = FakeFiles(
        entries=[
            _entry("data/pic.png", 4),  # 二进制
            _entry("blob.bin", 4 * 1024 * 1024),  # 超过默认 2MiB 上限
            _entry("keep.txt", 3),  # 应被拉回
        ],
        blobs={f"{MOUNT}/keep.txt": b"abc"},
    )

    changed = asyncio.run(sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path))

    assert changed == 1
    assert (tmp_path / "keep.txt").read_bytes() == b"abc"
    assert not (tmp_path / "data" / "pic.png").exists()
    assert [c[0] for c in files.read_calls] == [f"{MOUNT}/keep.txt"]


def test_b2_binary_opt_in_restores_old_behaviour(tmp_path: Path) -> None:
    """显式开启 pull_binaries 时，图片仍会被回拉（可配置回退）。"""
    files = FakeFiles(
        entries=[_entry("data/pic.png", 3)],
        blobs={f"{MOUNT}/data/pic.png": b"png"},
    )

    changed = asyncio.run(
        sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path, pull_binaries=True)
    )

    assert changed == 1
    assert (tmp_path / "data" / "pic.png").read_bytes() == b"png"


# ---- B2：熔断 + 显式短超时 ------------------------------------------------------------


def test_b2_circuit_breaker_stops_after_consecutive_failures(tmp_path: Path) -> None:
    """连续失败达阈值即中止本轮——最坏耗时从 N×60s 压到 阈值×超时。"""
    files = FakeFiles(
        entries=[_entry(f"f{i}.txt", 3) for i in range(6)],
        read_error=TimeoutError("timed out"),
    )

    changed = asyncio.run(
        sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path, failure_limit=2)
    )

    assert changed == 0
    assert len(files.read_calls) == 2  # 只尝试了 2 次就熔断，而不是 6 次


def test_b2_passes_explicit_short_timeout(tmp_path: Path) -> None:
    """必须显式传入 request_timeout（否则落到 SDK 默认 60s 整请求硬超时）。"""
    files = FakeFiles(
        entries=[_entry("gen.py", 3)], blobs={f"{MOUNT}/gen.py": b"abc"}
    )

    asyncio.run(
        sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path, read_timeout=7.5)
    )

    assert files.read_calls[0][1] == "bytes"
    assert files.read_calls[0][2] == 7.5


def test_b2_list_failure_is_non_fatal(tmp_path: Path) -> None:
    """list 失败（沙箱不可用）只告警并返回 0，不抛异常。"""
    files = FakeFiles(list_error=RuntimeError("sandbox down"))

    changed = asyncio.run(sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path))

    assert changed == 0


def test_b2_worst_case_cost_is_bounded(tmp_path: Path) -> None:
    """最坏耗时有界：500 个文件里，护栏短路掉全部 500 个 → 零远端读取。

    旧实现在这里会发起约 500 次远端读取（每个都可能各等一个 60s 超时）；
    新实现只做 1 次 list。
    """
    src = tmp_path / "src"
    src.mkdir()
    entries = []
    for i in range(480):
        rel = f"src/m{i}.py"
        (tmp_path / rel).write_bytes(b"x" * 8)  # 本地已有、大小一致
        entries.append(_entry(rel, 8))
    for i in range(20):
        entries.append(_entry(f"img/p{i}.png", 1024))  # 二进制

    files = FakeFiles(entries=entries)
    changed = asyncio.run(sync_sandbox_to_local(FakeSandbox(files), WS_ID, tmp_path))

    assert changed == 0
    assert files.read_calls == []  # 500 个文件 → 0 次读取，1 次 list


# ---- B3：免下载比对（用 list 尺寸表替代逐文件远端读取） -------------------------------


def test_b3_uses_size_map_and_never_reads_remote(tmp_path: Path) -> None:
    """推送侧不再逐文件 read 远端：一致的不传，不一致的直接传。"""
    (tmp_path / "same.txt").write_bytes(b"aaaa")
    (tmp_path / "diff.txt").write_bytes(b"bbbb")

    files = FakeFiles(
        entries=[
            _entry("same.txt", 4),
            _entry("diff.txt", 2),
        ]
    )

    changed = asyncio.run(
        sync_local_to_sandbox(FakeSandbox(files), WS_ID, tmp_path)
    )

    assert changed == 1
    assert files.read_calls == []  # 关键：零远端读取
    assert [p for p in files.write_calls] == [f"{MOUNT}/diff.txt"]


def test_b3_pushes_new_file_absent_remotely(tmp_path: Path) -> None:
    (tmp_path / "new.txt").write_bytes(b"hello")
    files = FakeFiles(entries=[])

    changed = asyncio.run(sync_local_to_sandbox(FakeSandbox(files), WS_ID, tmp_path))

    assert changed == 1
    assert files.write_calls == [f"{MOUNT}/new.txt"]


def test_b3_circuit_breaker_on_write_failures(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"abc")

    class FailingWrite(FakeFiles):
        async def write(self, path: str, data) -> None:
            self.write_calls.append(path)
            raise TimeoutError("write timed out")

    files = FailingWrite(entries=[])
    changed = asyncio.run(
        sync_local_to_sandbox(FakeSandbox(files), WS_ID, tmp_path, failure_limit=2)
    )

    assert changed == 0
    assert len(files.write_calls) == 2
