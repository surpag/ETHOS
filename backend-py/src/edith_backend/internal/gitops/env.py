"""git 非交互环境：防止沙箱内 git 卡死等 stdin / 打开分页器 / 弹出编辑器。

对应 wuu `internal/tools/toolkit.go` 的 `nonInteractiveShellEnv`。
`GIT_TERMINAL_PROMPT=0` 是必须项——否则需要认证的 `git pull` 会挂住等凭据输入，
任务空转、计费照跑。执行时以 `envs=` 覆盖式合并（E2B 对同名键覆盖、保留其余）。
"""

from __future__ import annotations

import os

NON_INTERACTIVE_ENV = {
    # 禁交互式编辑器
    "EDITOR": "true",
    "VISUAL": "true",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    # 禁分页器（否则 git log/diff 会进入 less 挂起）
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GH_PAGER": "cat",
    # ★ 禁凭据提示（关键：避免需认证时挂住等 stdin）
    "GIT_TERMINAL_PROMPT": "0",
    # 去颜色，输出更干净
    "NO_COLOR": "1",
}

# 平台默认提交身份（用户未设置时使用）
DEFAULT_AUTHOR_NAME = "EDITH Agent"
DEFAULT_AUTHOR_EMAIL = "agent@edith.local"


def non_interactive_env() -> dict[str, str]:
    """返回 git 执行时的非交互环境覆盖（调用方以 envs= 合并，覆盖同名键）。"""
    return dict(NON_INTERACTIVE_ENV)


def resolve_git_identity(name: str = "", email: str = "") -> tuple[str, str]:
    """解析提交身份：用户设置优先，留空回退环境变量，再回退平台默认。

    环境变量在调用时读取（而非 import 时），避免 .env 加载顺序影响取值。
    """
    resolved_name = (
        (name or "").strip()
        or os.getenv("EDITH_GIT_AUTHOR_NAME", "").strip()
        or DEFAULT_AUTHOR_NAME
    )
    resolved_email = (
        (email or "").strip()
        or os.getenv("EDITH_GIT_AUTHOR_EMAIL", "").strip()
        or DEFAULT_AUTHOR_EMAIL
    )
    return resolved_name, resolved_email


def git_identity_env(name: str, email: str) -> dict[str, str]:
    """提交身份的环境变量覆盖（author 与 committer 一致）。

    用环境变量而非 `git -c user.name=...`：`-c` 在 BLOCKED_COMMIT_FLAGS 里被禁用
    （防任意 config 注入），且 env 不落盘、不进命令行。
    """
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
