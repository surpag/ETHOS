"""项目级 Skill 的归属校验（设计稿 7.5 / D7）。

为什么判定必须在存储层
--------------------
`sandbox/workspace.py` 的 `workspace_path()` 校验的是「这条路径与当前会话的
workspace 上下文是否一致」，**不是**「这个 workspace 属于这个 user」。
沙箱内 `sandbox_run_command` 可以直接 `cp`/`mv`/`rm`，完全不过工具层的路径校验，
所以路径校验只是纵深防御的第二层，第一层必须在拿到权威副本之前。

三条硬性性质
------------
1. **fail-closed**：没有注入判定器时**拒绝**，不是放行。少一次配置不该变成
   「所有项目 Skill 对所有用户可见」。
2. **不缓存**：SQLite 上一条 `SELECT 1` 是微秒级；进程内缓存会在权限被收回后
   继续放行，且多副本之间不一致。
3. **不区分「不存在」与「无权」**：对外统一 `workspace not accessible`，
   否则等于提供了一个探测他人 workspace 是否存在的接口。

唯一入口是 `assert_access(user_id, workspace_id)`；四个必须校验的点
（读索引 / push / pull / 删除或迁移）都经由后端的 `assert_access()` 方法走到这里，
不给调用方「忘了校验」的机会。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: 判定器签名：给出 (user_id, workspace_id)，回答「是否属于你」
AccessChecker = Callable[[str, str], Awaitable[bool]]

#: 对外统一的拒绝原因（刻意不区分「不存在」与「不属于你」）
DENIED_REASON = "workspace not accessible"
#: 没有注入判定器时的原因：这是装配缺陷，必须与「确实无权」区分开
UNCONFIGURED_REASON = "workspace access check not configured"


class SkillAccessDenied(Exception):
    """无权访问该作用域的存储。

    刻意**不继承** `OSError`：`read_index` / `push` / `pull` 用
    `except (OSError, ValueError)` 兜底本地文件错误，权限拒绝若落进那个分支，
    就会被写成「ensure skill dir failed: ...」这类误导性原因。
    """

    def __init__(self, reason: str = DENIED_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


def workspace_access_checker(store) -> AccessChecker:
    """用 `WorkspaceStore` 构造判定器（`SELECT 1 ... WHERE user_id AND workspace_id`）。"""

    async def check(user_id: str, workspace_id: str) -> bool:
        if not user_id or not workspace_id:
            return False
        return await store.exists(user_id, workspace_id)

    return check


async def assert_access(
    checker: AccessChecker | None, user_id: str, workspace_id: str
) -> None:
    """唯一入口：无权、入参不完整或未配置判定器时抛 `SkillAccessDenied`。"""
    if not user_id or not workspace_id:
        # 空 id 拼出来的路径会落到"根目录"上，属于调用方 bug，必须拒绝而不是放行
        logger.warning(
            "skill access check with empty id user=%r ws=%r", user_id, workspace_id
        )
        raise SkillAccessDenied(DENIED_REASON)
    if checker is None:
        logger.warning(
            "project skill access checker missing ws=%s (denying by default)",
            workspace_id,
        )
        raise SkillAccessDenied(UNCONFIGURED_REASON)
    if not await checker(user_id, workspace_id):
        logger.warning(
            "project skill access denied user=%s ws=%s", user_id[:12], workspace_id
        )
        raise SkillAccessDenied(DENIED_REASON)
