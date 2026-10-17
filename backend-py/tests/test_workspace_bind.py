"""绑定本地目录（方案 B）的存量合并策略测试。

覆盖三件套：
1. `plan_merge` / `merge_dir_add_only`：只新增不覆盖 + 忽略过滤（与日常同步对齐）
2. `_detect_local_root_prefix`：放宽判定（卷根存在同名子目录即命中，不要求唯一）
3. `WorkspaceVolumeService.bind_local_root`：默认不合并、显式合并、回报未合并数
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from edith_backend.internal.workspace.http import _detect_local_root_prefix
from edith_backend.internal.workspace.sync import merge_dir_add_only, plan_merge
from edith_backend.internal.workspace.volume_backend import LocalVolumeBackend
from edith_backend.internal.workspace.volume_service import (
    BindOutcome,
    WorkspaceVolumeService,
)


class FakeStore:
    """只记录 bind_local_root 调用，避免单元测试依赖数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def bind_local_root(
        self, user_id: str, workspace_id: str, local_root: str, prefix: str
    ) -> None:
        self.calls.append((user_id, workspace_id, local_root, prefix))


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---- 1. plan_merge / merge_dir_add_only -------------------------------------------------


def test_plan_merge_skips_existing_and_ignored(tmp_path: Path) -> None:
    """规划只包含「本地没有」且「未被忽略」的文件。"""
    src = tmp_path / "legacy"
    dst = tmp_path / "local"
    _write(src, "keep.txt")
    _write(src, "docs/report.md")
    _write(src, "already.txt", "old")
    _write(dst, "already.txt", "user")
    # 忽略项：内置 DEFAULT_IGNORES 覆盖版本库/构建产物
    _write(src, ".git/config")
    _write(src, "node_modules/pkg/index.js")
    _write(src, "__pycache__/mod.pyc")

    assert sorted(plan_merge(src, dst)) == ["docs/report.md", "keep.txt"]


def test_merge_dir_add_only_applies_plan_without_overwriting(tmp_path: Path) -> None:
    """实际合并 = 按 plan 复制；既有文件内容不变；忽略项不落盘。"""
    src = tmp_path / "legacy"
    dst = tmp_path / "local"
    _write(src, "keep.txt", "new")
    _write(src, "already.txt", "old")
    _write(dst, "already.txt", "user")
    _write(src, "node_modules/pkg/index.js")

    copied = merge_dir_add_only(src, dst)

    assert copied == 1
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (dst / "already.txt").read_text(encoding="utf-8") == "user"  # 不覆盖
    assert not (dst / "node_modules").exists()


def test_plan_merge_honors_prefix(tmp_path: Path) -> None:
    """前缀命中时只取该子树，并剥掉前缀映射到本地根。"""
    src = tmp_path / "legacy"
    dst = tmp_path / "local"
    _write(src, "Data/x.txt")
    _write(src, "Data/sub/y.txt")
    _write(src, "other.txt")

    assert sorted(plan_merge(src, dst, prefix="Data")) == ["sub/y.txt", "x.txt"]
    assert plan_merge(src, dst, prefix="Missing") == []


# ---- 2. _detect_local_root_prefix -------------------------------------------------------


class _FakeService:
    def __init__(self, backend: LocalVolumeBackend) -> None:
        self.backend = backend


def test_detect_prefix_matches_named_dir_among_others(tmp_path: Path) -> None:
    """放宽后：卷根还混有别的条目（报告/缓存）时，仍能命中同名子目录。"""
    backend = LocalVolumeBackend(tmp_path / "volumes")
    legacy = backend.legacy_dir("w1")
    _write(legacy, "Data/x.txt")
    _write(legacy, "summary.md")  # 绑定前 Agent 的产物
    _write(legacy, ".pytest_cache/CACHEDIR.TAG")

    local_root = tmp_path / "Data"
    local_root.mkdir()

    assert _detect_local_root_prefix(_FakeService(backend), "w1", local_root) == "Data"


def test_detect_prefix_empty_when_no_match(tmp_path: Path) -> None:
    """卷根没有同名子目录 → 空前缀（不做猜测）。"""
    backend = LocalVolumeBackend(tmp_path / "volumes")
    legacy = backend.legacy_dir("w1")
    _write(legacy, "mp/agent-learn/index.ts")
    _write(legacy, "summary.md")

    local_root = tmp_path / "agent-learn"
    local_root.mkdir()

    assert _detect_local_root_prefix(_FakeService(backend), "w1", local_root) == ""


# ---- 3. bind_local_root 的合并开关 -------------------------------------------------------


def test_bind_local_root_defaults_to_no_merge(tmp_path: Path) -> None:
    """默认不合并：文件不搬，但回报「还有 N 个未合并」。"""
    backend = LocalVolumeBackend(tmp_path / "volumes")
    service = WorkspaceVolumeService(FakeStore(), backend)  # type: ignore[arg-type]
    legacy = backend.legacy_dir("w1")
    _write(legacy, "a.txt")
    _write(legacy, "docs/b.md")

    dst = tmp_path / "local"
    dst.mkdir()

    outcome = asyncio.run(service.bind_local_root("u1", "w1", str(dst)))

    assert outcome == BindOutcome(merged_files=0, unmerged_files=2)
    assert list(dst.rglob("*")) == []  # 一个文件都没搬
    # 绑定本身仍然生效
    assert backend.local_root_of("w1") == dst


def test_bind_local_root_merges_when_requested(tmp_path: Path) -> None:
    """显式 merge_existing=True 时才合并（只新增不覆盖），之后未合并数归零。"""
    backend = LocalVolumeBackend(tmp_path / "volumes")
    service = WorkspaceVolumeService(FakeStore(), backend)  # type: ignore[arg-type]
    legacy = backend.legacy_dir("w1")
    _write(legacy, "a.txt", "agent")
    _write(legacy, "keep.txt", "agent")
    _write(legacy, "node_modules/pkg/index.js")

    dst = tmp_path / "local"
    _write(dst, "keep.txt", "user")

    outcome = asyncio.run(
        service.bind_local_root("u1", "w1", str(dst), merge_existing=True)
    )

    assert outcome == BindOutcome(merged_files=1, unmerged_files=0)
    assert (dst / "a.txt").read_text(encoding="utf-8") == "agent"
    assert (dst / "keep.txt").read_text(encoding="utf-8") == "user"  # 不覆盖
    assert not (dst / "node_modules").exists()
