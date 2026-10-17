"""git 执行器：在沙箱内以非交互环境执行白名单内的 git 命令，并脱敏输出。

只读 + clone + 本地写（add/commit）默认可用；远端写（push/pull/fetch）需要 runner 沙箱
（凭据只在那里可达，见 policy 的 allow_remote）。
"""

from __future__ import annotations

import shlex

from e2b.sandbox.commands.command_handle import CommandExitException

from .env import git_identity_env, non_interactive_env
from .policy import GitCommandRejected, validate_git_command
from .redact import redact_git_output
from .ssrf import assert_safe_clone_url

# git 单次执行超时（秒）
DEFAULT_GIT_TIMEOUT = 60


async def run_git(
    sandbox,
    workspace_path: str,
    subcmd: str,
    args: list[str],
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT,
    identity: tuple[str, str] | None = None,
    allow_remote: bool = False,
) -> dict:
    """执行一次白名单内的 git 调用，返回 {exitCode, stdout, stderr, command}。

    顺序：白名单校验 → clone 额外做 SSRF 校验 → 组装命令（shlex.quote 防注入）
    → 非交互 env（+ 提交身份）→ 执行 → 输出脱敏。

    identity=(name, email) 时注入 GIT_AUTHOR_*/GIT_COMMITTER_*（用 env 而非 `-c`，
    后者在 BLOCKED_COMMIT_FLAGS 内被禁）。
    allow_remote=True 才放行 push/pull/fetch —— 只应由 runner 沙箱的调用方设置。

    git 大量合法场景以**非零退出码**表达（非仓库=128、冲突=1…），E2B 的 commands.run
    会把非零退出抛成 CommandExitException（它同时是 CommandResult 子类，带 exit_code /
    stdout / stderr）——这里统一转回结果，交由上层按 exit_code 判断。
    """
    validate_git_command(subcmd, args, allow_remote=allow_remote)
    if subcmd == "clone" and args:
        assert_safe_clone_url(args[0])

    command = "git " + " ".join([subcmd, *(shlex.quote(str(a)) for a in args)])
    envs = non_interactive_env()
    if identity is not None:
        envs.update(git_identity_env(*identity))
    try:
        result = await sandbox.commands.run(
            command,
            cwd=workspace_path,
            envs=envs,
            timeout=timeout,
        )
    except CommandExitException as exc:
        result = exc  # 非零退出 = 正常 git 结果，不是 gitops 层故障
    return {
        "exitCode": result.exit_code or 0,
        "stdout": redact_git_output(result.stdout or ""),
        "stderr": redact_git_output(result.stderr or ""),
        "command": command,
    }


__all__ = [
    "GitCommandRejected",
    "assert_safe_clone_url",
    "git_identity_env",
    "non_interactive_env",
    "redact_git_output",
    "run_git",
    "validate_git_command",
]
