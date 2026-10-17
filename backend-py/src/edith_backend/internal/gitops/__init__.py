"""gitops：云端工作区 Git 远程管理（安全护栏 + 执行器）。

对应 `docs/cloud-multitenant-migration.md` §五/§六。
M2 落地安全护栏 + 只读执行器；M3 放开本地写（add/commit）；远端写（push）由
`orchestrator.WorkspacePush` 在后端带凭据完成，Agent 工具 `git_push` 与 HTTP
`POST /internal/git/push` 共用同一份实现（沙箱内永远没有凭据）。
"""

from .env import git_identity_env, non_interactive_env, resolve_git_identity
from .executor import run_git
from .policy import GitCommandRejected, validate_git_command
from .redact import redact_git_output
from .ssrf import CloneUrlRejected, assert_safe_clone_url

__all__ = [
    "CloneUrlRejected",
    "GitCommandRejected",
    "assert_safe_clone_url",
    "git_identity_env",
    "non_interactive_env",
    "redact_git_output",
    "resolve_git_identity",
    "run_git",
    "validate_git_command",
]
