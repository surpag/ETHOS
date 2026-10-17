"""后端侧 push：在本地临时仓库里 fetch bundle 并推送到远端。

凭据只经**环境变量**注入本地 git 进程：
`GIT_CONFIG_COUNT=1` / `GIT_CONFIG_KEY_0=http.extraHeader` /
`GIT_CONFIG_VALUE_0="Authorization: Basic ..."` —— 不落盘、不进命令行（`ps` 看不到）、
不进入任何沙箱。remote URL 保持干净（不含 token），因此不会被写进 `.git/config`。
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from pathlib import Path

from .redact import redact_git_output

PUSH_TIMEOUT = 300  # 秒；大仓首次推送可能较久


class PushError(Exception):
    """push 失败（可读原因，且已脱敏）。"""


class PushNonFastForward(PushError):
    """远端有本地没有的提交（或历史完全不相关），普通推送会被拒。

    单列一个类型，让上层能给出「该用户做什么」的指引（换新分支 / 先合并），
    而不是把 git 的 stderr 原样丢给模型去猜。**任何情况下都不做强推。**
    """


# git 在「远端有本地没有的提交」时的典型输出（英文/本地化混杂，取常见几个）
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "failed to push some refs",
    "updates were rejected",
    "tip of your current branch is behind",
)


def _is_non_fast_forward(stderr: str) -> bool:
    text = stderr.lower()
    return any(marker in text for marker in _NON_FAST_FORWARD_MARKERS)


# 分支名合法性：必须是单个 ref 名，不能带选项前缀/控制字符/ref 元字符，
# 否则 `git push <url> refs/heads/x:refs/heads/y` 的 y 可能被当成选项解析。
_REF_FORBIDDEN = set(" ~^:?*[\\")


def validate_branch_name(name: str) -> str:
    """校验并返回分支名；非法时抛 PushError。

    这是模型可控的输入（工具参数），必须挡在拼进 git 命令之前。
    """
    if not name:
        raise PushError("缺少分支名")
    if name.startswith(("-", "/")) or name.endswith("/"):
        raise PushError(f"非法的分支名：{name!r}")
    if name.startswith(".") or name.endswith(".") or ".." in name or "@{" in name:
        raise PushError(f"非法的分支名：{name!r}")
    if any(ch in _REF_FORBIDDEN or ord(ch) < 32 for ch in name):
        raise PushError(f"非法的分支名：{name!r}")
    return name


def _auth_env(token: str) -> dict[str, str]:
    """GitHub HTTPS 认证：任意用户名 + token 作密码，经 http.extraHeader 注入。"""
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
    }


async def _run_git(args: list[str], cwd: str, token: str | None) -> tuple[int, str, str]:
    """在本机执行 git（非交互、无系统配置），返回 (exitCode, stdout, stderr)。"""
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # 禁凭据提示（否则会挂住等 stdin）
            "GIT_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    if token:
        env.update(_auth_env(token))
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=PUSH_TIMEOUT)
    except TimeoutError as exc:
        proc.kill()
        raise PushError(f"git {args[0]} 超时（>{PUSH_TIMEOUT}s）") from exc
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        redact_git_output(err.decode("utf-8", errors="replace")),
    )


async def push_bundle(
    bundle: bytes,
    remote_url: str,
    branch: str,
    token: str,
    *,
    target_branch: str = "",
) -> dict:
    """把 bundle 里 `branch` 的提交推送到远端 `target_branch`。

    源分支与目标分支**解耦**：`refs/heads/<branch>:refs/heads/<target>`。
    这样「本地 master → 远端 main」「本地 master → 远端 docs/readme」都能表达；
    早先强制同名（x:x）导致任何分支名不一致的场景都推不上去。

    步骤：临时目录 git init（bare）→ fetch bundle 到本地分支 → 带凭据 push。
    非快进抛 PushNonFastForward，不做任何强推。
    """
    if not bundle:
        raise PushError("bundle 为空，没有可推送的提交")
    branch = validate_branch_name(branch)
    target = validate_branch_name(target_branch or branch)
    if not token:
        raise PushError("未配置 Git 凭据，请先在设置中连接 GitHub")

    with tempfile.TemporaryDirectory(prefix="edith-push-") as tmp:
        bundle_path = Path(tmp) / "incoming.bundle"
        bundle_path.write_bytes(bundle)

        # 用 **bare** 临时仓库接收：没有 checked-out 的分支，fetch 到任意
        # refs/heads/* 都不会被 git 拒绝（非 bare 时会报
        # "refusing to fetch into branch 'refs/heads/<b>' checked out at ..."）。
        code, _, err = await _run_git(["init", "--bare", "-q"], tmp, None)
        if code != 0:
            raise PushError(f"git init 失败：{err[:200]}")

        code, _, err = await _run_git(
            ["fetch", str(bundle_path), f"refs/heads/{branch}:refs/heads/{branch}"],
            tmp,
            None,
        )
        if code != 0:
            raise PushError(f"从 bundle 载入提交失败：{err[:200]}")

        code, _, err = await _run_git(
            ["push", remote_url, f"refs/heads/{branch}:refs/heads/{target}"],
            tmp,
            token,
        )
        if code != 0:
            detail = err[:300]
            if _is_non_fast_forward(err):
                raise PushNonFastForward(f"push 被拒（非快进）：{detail}")
            raise PushError(f"push 失败：{detail}")

    return {"branch": branch, "remoteBranch": target, "remoteUrl": remote_url}


async def list_remote_branches(remote_url: str, token: str) -> dict:
    """只读查询远端有哪些分支、默认分支是哪个。

    Agent 在推送前需要知道「远端已经有什么」，否则只能凭猜（真实事故：模型
    看到本地分支叫 master，就断言「目标 main 推不上去是工具限制」）。
    用 `ls-remote` 而非 clone：一次网络往返，不落地、不写盘。

    返回 {"branches": [{"name","sha"}], "defaultBranch": "master", "remoteUrl": url}
    """
    if not remote_url:
        raise PushError("缺少远端仓库地址")
    with tempfile.TemporaryDirectory(prefix="edith-lsremote-") as tmp:
        code, out, err = await _run_git(
            ["ls-remote", "--heads", remote_url], tmp, token
        )
        if code != 0:
            raise PushError(f"读取远端分支失败：{err[:300]}")

        branches = []
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                branches.append({"name": ref[len("refs/heads/") :], "sha": sha})

        # 默认分支：--symref 会给出 `ref: refs/heads/xxx  HEAD`
        default_branch = ""
        code, out, _ = await _run_git(
            ["ls-remote", "--symref", remote_url, "HEAD"], tmp, token
        )
        if code == 0:
            for line in out.splitlines():
                if line.startswith("ref:") and line.endswith("HEAD"):
                    ref = line[len("ref:") :].strip().split()[0]
                    if ref.startswith("refs/heads/"):
                        default_branch = ref[len("refs/heads/") :]

    return {
        "remoteUrl": remote_url,
        "defaultBranch": default_branch,
        "branches": branches,
    }
