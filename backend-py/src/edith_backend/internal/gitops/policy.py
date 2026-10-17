"""git 命令白名单（默认拒绝）。

对应 wuu `internal/tools/git.go` 的三层校验：
- 第一层 全局拦截：`--config-env` / `--exec-path`（可让 git 加载任意可执行文件或环境变量，
  是提权入口）、shell 元字符（每条参数经 shlex.quote 传递本无注入，但拦截可防止模型写出
  类似 shell 的花式用法）
- 第二层 子命令级：只读 + clone + 本地写（add/commit）默认放行；远端写（push/pull/fetch）
  仅当 `allow_remote=True`（只有 runner 沙箱可设，凭据只在那里可达）
- 第三层 参数级：add 只收显式路径；commit 必须带消息且禁审计敏感 flag；远端禁 force 等

默认拒绝意味着：漏登记 = 越权，而不是漏拦 = 越权。
"""

from __future__ import annotations

# 纯只读子命令：不改变工作区、索引、远端
READ_ONLY_SUBCOMMANDS = frozenset({
    "log", "show", "diff", "status", "blame", "reflog",
    "ls-files", "ls-remote", "rev-parse", "rev-list", "describe",
    "cat-file", "for-each-ref", "grep", "merge-base", "shortlog",
    "bundle",  # 只读：把对象打包成文件，不改变仓库（D-2 导出提交用）
})

# 导入类：允许，但 URL 需过 SSRF 校验（见 executor）
IMPORT_SUBCOMMANDS = frozenset({"clone"})

# 本地写子命令（不需要凭据）：Agent 沙箱内可直接执行
WRITE_SUBCOMMANDS = frozenset({"add", "commit"})

# 远端子命令（需要凭据）：只在 runner 沙箱放行（allow_remote=True）
REMOTE_SUBCOMMANDS = frozenset({"push", "pull", "fetch"})

# 全局拦截的参数前缀（任何子命令都禁）
BLOCKED_GLOBAL_ARG_PREFIXES = ("--config-env", "--exec-path")

# shell 元字符：作为纵深防御拦截（即使 shlex.quote 已能防注入）
SHELL_METACHARS = frozenset(";&|$`><()")

# 危险 commit flag（审计生命线）：--no-verify 会跳过 hook（= 绕过平台服务端校验）；
# --author/--date 可伪造归属；-c/-C 可复用任意 commit 的消息/作者；--gpg-sign 等签名相关一律禁。
BLOCKED_COMMIT_FLAGS = frozenset({
    "--no-verify", "--gpg-sign", "--no-gpg-sign", "-S",
    "--signoff", "--fixup", "--squash", "-C", "-c", "--author", "--date",
})

# `add` 只接受显式路径（抄 wuu）：拒绝 `.` / `-A` / 通配符，避免误暂存敏感或无关文件。
_BLOCKED_ADD_ARGS = frozenset({
    ".", "-A", "--all", "-u", "--update", "--interactive", "-i", "--patch", "-p",
})

# 远端子命令禁用的危险 flag：强推、删远端分支、镜像、跳过 hook。
_BLOCKED_REMOTE_FLAGS = frozenset({
    "--force", "-f", "--force-with-lease", "--force-if-includes",
    "--mirror", "--delete", "-d", "--prune", "--tags", "--all", "--no-verify",
})


class GitCommandRejected(Exception):
    """git 调用被白名单拒绝（可读原因供模型/日志）。"""


def validate_git_command(
    subcmd: str, args: list[str], *, allow_remote: bool = False
) -> None:
    """校验一次 git 调用；非法时抛 GitCommandRejected。

    allow_remote=True 额外放行 push/pull/fetch —— 只有 runner 沙箱（凭据可达）可设。
    """
    if not subcmd:
        raise GitCommandRejected("git subcommand is required")
    allowed = READ_ONLY_SUBCOMMANDS | IMPORT_SUBCOMMANDS | WRITE_SUBCOMMANDS
    if allow_remote:
        allowed = allowed | REMOTE_SUBCOMMANDS
    if subcmd not in allowed:
        raise GitCommandRejected(f"git subcommand {subcmd!r} is not allowed")

    for arg in args:
        if arg.startswith(BLOCKED_GLOBAL_ARG_PREFIXES):
            raise GitCommandRejected(f"git arg {arg!r} is not allowed")
        if any(ch in SHELL_METACHARS for ch in arg):
            raise GitCommandRejected(f"git arg {arg!r} contains shell metacharacters")

    if subcmd == "add":
        _validate_add(args)
    elif subcmd == "commit":
        _validate_commit(args)
    elif subcmd in REMOTE_SUBCOMMANDS:
        _validate_remote(subcmd, args)


def _validate_add(args: list[str]) -> None:
    """git add 只接受显式路径：拒绝 '.'/`-A`/通配符。"""
    if not args:
        raise GitCommandRejected("git add requires explicit paths")
    for arg in args:
        if arg.startswith("-") or "*" in arg or arg in _BLOCKED_ADD_ARGS:
            raise GitCommandRejected(f"git add only accepts explicit paths, got {arg!r}")


def _validate_commit(args: list[str]) -> None:
    """git commit 必须带显式消息，并禁用审计敏感的 flag。"""
    has_message = any(
        a in ("-m", "--message", "-F", "--file")
        or a.startswith(("--message=", "--file="))
        for a in args
    )
    if not has_message:
        raise GitCommandRejected("git commit requires an explicit message (-m)")
    for arg in args:
        if arg.split("=", 1)[0] in BLOCKED_COMMIT_FLAGS:
            raise GitCommandRejected(f"git commit flag {arg!r} is not allowed")


def _validate_remote(subcmd: str, args: list[str]) -> None:
    """远端子命令禁用 force / 删远端 / 镜像 / 跳过 hook 等危险 flag。"""
    for arg in args:
        if arg.split("=", 1)[0] in _BLOCKED_REMOTE_FLAGS:
            raise GitCommandRejected(f"git {subcmd} flag {arg!r} is not allowed")
