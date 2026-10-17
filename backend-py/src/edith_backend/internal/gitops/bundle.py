"""从沙箱导出提交：`git bundle`（供后端在凭据隔离下 push）。

设计（D-2）：Agent 沙箱**没有凭据**，只负责把本地提交打包成 bundle；
后端读出 bundle，在自己的临时目录 fetch 后带凭据 push。
选 bundle 而非 `format-patch`：bundle 保留完整 commit 对象（含 merge 结构与历史），
`git fetch` 后与真实仓库一致；patch 会丢 merge、易出冲突。
"""

from __future__ import annotations

import logging
import uuid

from .executor import run_git
from .policy import GitCommandRejected

logger = logging.getLogger(__name__)

# 打包可能涉及大量对象，给足超时
BUNDLE_TIMEOUT = 180


class BundleExportError(Exception):
    """沙箱内导出 bundle 失败（可读原因）。"""


async def current_branch(sandbox, workspace_path: str) -> str:
    """返回工作区仓库当前分支名；非仓库/游离 HEAD 时回退空串。"""
    result = await run_git(sandbox, workspace_path, "rev-parse", ["--abbrev-ref", "HEAD"])
    name = (result["stdout"] or "").strip()
    return "" if name in ("", "HEAD") else name


async def export_bundle(
    sandbox, workspace_path: str, branch: str = ""
) -> bytes:
    """把工作区仓库打包为 bundle 并返回字节。

    branch 为空时用 `--all`（全部 refs），保证任何分支的提交都能带出来。
    """
    tmp = f"/tmp/edith-push-{uuid.uuid4().hex}.bundle"
    rev = branch.strip() or "--all"
    try:
        result = await run_git(
            sandbox,
            workspace_path,
            "bundle",
            ["create", tmp, rev],
            timeout=BUNDLE_TIMEOUT,
        )
    except GitCommandRejected as exc:
        raise BundleExportError(str(exc)) from exc
    if result["exitCode"] != 0:
        raise BundleExportError(
            f"git bundle 失败（exit {result['exitCode']}）：{result['stderr'][:200]}"
        )

    try:
        data = await sandbox.files.read(tmp, format="bytes")
    finally:
        try:
            await sandbox.commands.run(f"rm -f {tmp}")
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响结果
            logger.debug("cleanup bundle temp failed: %s", exc)

    if not data:
        raise BundleExportError("bundle 为空（工作区可能还不是 git 仓库，或没有任何提交）")
    return bytes(data)
