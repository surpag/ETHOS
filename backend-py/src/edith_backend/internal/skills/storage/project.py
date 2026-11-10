"""项目级 Skill 的本地后端：**独立根** + 沙箱路径带 `workspace_id`。

为什么不能塞进用户级根目录之下
------------------------------
用户级 `push` 是把「整个用户根目录」推到 `skills/custom`（`local.py` 的 `push`）。
若项目级放在 `user-skills/{user_id}/{workspace_id}/`：

1. 用户级 push 会把项目 Skill **一起推走**，落到别人的 `skills/custom` 里；
2. `_scan` 只扫一层目录，`{workspace_id}` 会被当成一个「没有 SKILL.md 的目录」静默跳过；
3. `state_key` 共用同一份共识集合，删除传播会把「删项目级」推演成「用户级也删了」，
   而 D3 的「项目级删除后恢复用户级」正是靠两套集合互不干扰。

所以它是 `project-skills/{user_id}/{workspace_id}/`，与 `user-skills/` 平级。

为什么要绑定 `workspace_id` 而不是每次调用传入
---------------------------------------------
`SkillStorageBackend` 的方法签名只有 `user_id`（设计稿 7.3 的契约：用户级调用点不该
被迫传一个假的 `workspace_id`）。项目级后端因此在**构造时**绑定一个 workspace，
一个实例只服务一个项目；`SkillScopeStores` 负责按 workspace 缓存实例，保住
基线指纹与快路径。

沙箱路径同理：`skills/project/{workspace_id}/{name}`。沙箱按 `(user, session)` 复用，
同一沙箱可能先后服务两个 workspace——路径带 ID 之后，残留目录只会占空间，
不会被当成当前项目的 Skill 读走（设计稿 7.2）。
"""

from __future__ import annotations

import os
import posixpath

from ...workspace.layout import WORKSPACE
from .access import AccessChecker, assert_access
from .base import SANDBOX_PROJECT_ROOT
from .local import LocalSkillBackend, _safe_segment

# 本地项目级 Skill 根目录（与 user-skills/ 平级，环境变量可覆盖）
_DEFAULT_ROOT = "project-skills"
_ROOT_ENV = "EDITH_PROJECT_SKILLS_DIR"

# 共识集合前缀刻意与用户级不同名：两套集合一旦同名，删除传播会越界。
_STATE_PREFIX = "project-skills-"


def project_sandbox_root(workspace_id: str) -> str:
    """沙箱内某工作区的项目级 Skill 根目录（绝对值）。

    抽成纯函数而不是只留在实例里：解析器要拼索引里的**路径行**，而路径必须与
    `push` 真正写入的目录同源。两边各算一次，规范化差一个字符就是"模型读到空
    Skill"——它不会报错。
    """
    return f"{SANDBOX_PROJECT_ROOT}/{_safe_segment((workspace_id or '').strip())}"


def project_index_base(workspace_id: str) -> str:
    """索引「路径」行用的工作区相对目录（`skills/project/{ws}`）。"""
    return posixpath.relpath(project_sandbox_root(workspace_id), WORKSPACE.root)


class ProjectSkillBackend(LocalSkillBackend):
    """单个 workspace 的项目级 Skill 后端（机制全部复用用户级）。"""

    name = "local-project"

    def __init__(
        self,
        workspace_id: str,
        root: str | None = None,
        *,
        access_checker: AccessChecker | None = None,
    ) -> None:
        raw = (workspace_id or "").strip()
        if not raw:
            # 空 workspace 不是「没有项目 Skill」，而是调用方搞错了作用域。
            # 静默降级会把配置错误伪装成「这个项目还没有 Skill」。
            raise ValueError("project scope requires a non-empty workspace_id")
        workspace = _safe_segment(raw)
        resolved = root or os.getenv(_ROOT_ENV) or _DEFAULT_ROOT
        super().__init__(
            resolved,
            sandbox_root=project_sandbox_root(raw),
            state_prefix=f"{_STATE_PREFIX}{workspace}-",
        )
        self._workspace = workspace
        # 归属判定的唯一来源；None 时 fail-closed（见 access.assert_access）
        self._access_checker = access_checker

    @property
    def workspace_id(self) -> str:
        """规范化后的 workspace 段（索引路径与沙箱目录都用它，两者必须同源）。"""
        return self._workspace

    async def assert_access(self, user_id: str) -> None:
        """读索引 / push / pull / 删除都必须先过这里。

        判定在**存储层**而不是路径层：沙箱内 `run_command` 可以 `cp`/`mv`/`rm`，
        完全不过工具层的路径校验。路径校验只是纵深防御的第二层。
        """
        await assert_access(self._access_checker, user_id, self._workspace)

    def user_dir(self, user_id: str):  # type: ignore[override]
        """项目级的权威副本目录：`{root}/{user_id}/{workspace_id}/`。

        方法名沿用父类的 `user_dir`（push/pull 都以它为准），语义是「这个用户
        在本作用域下的根目录」——项目级多一层 workspace。
        """
        return self._root / _safe_segment(user_id) / self._workspace
