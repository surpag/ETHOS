"""用户 Skill 存储：后端抽象 + 门面 + 工厂。

对应环境变量 `EDITH_USER_SKILLS_BACKEND`：
- `local`（默认）：宿主目录 `user-skills/{user_id}/`，沙箱目录靠同步
- `e2b`：挂载 E2B Volume（需要账号已开通 Volumes private beta）

未知取值回退 `local` 并告警——与 `workspace/volume_backend.py:new_backend`
保持同一种失败姿态：宁可降级到能用的实现，也不因为拼错一个环境变量而启动即崩。

实现模块按需导入（`local` 依赖 `skills.catalog` 的 SKILL.md 解析，而
`skills.catalog` 又要引用本包的 `SkillIndex`）：本包初始化时若急切导入
`local`，会形成 `catalog → storage.__init__ → local → catalog` 的循环。
因此 `base` / `service` 才是常驻依赖，具体后端在工厂里延迟导入。
"""

from __future__ import annotations

import logging

from .access import SkillAccessDenied, assert_access, workspace_access_checker
from .base import (
    OVERVIEW_FILENAME,
    SANDBOX_PROJECT_ROOT,
    SANDBOX_SKILLS_PATH,
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    MountSpec,
    SkillIndex,
    SkillStorageBackend,
    SkillSyncResult,
)
from .scopes import SkillScopeStores, new_scope_backend
from .service import SkillStorageService

logger = logging.getLogger(__name__)


def new_skill_backend(kind: str, volumes=None) -> SkillStorageBackend:
    """按环境变量构造后端；`e2b` 需要传入 Volume Service。"""
    from .local import LocalSkillBackend

    kind = (kind or "").strip().lower()
    if kind == "e2b":
        if volumes is None:
            logger.warning("skill backend=e2b requires volume service; fallback to local")
        else:
            from .e2b import E2BSkillBackend

            logger.info("user skill backend: e2b (volume mount)")
            return E2BSkillBackend(volumes)
    elif kind and kind != "local":
        logger.warning("unknown EDITH_USER_SKILLS_BACKEND=%r, fallback to local", kind)
    logger.info("user skill backend: local (host dir)")
    return LocalSkillBackend()


__all__ = [
    "OVERVIEW_FILENAME",
    "SANDBOX_PROJECT_ROOT",
    "SANDBOX_SKILLS_PATH",
    "STATUS_EMPTY",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "MountSpec",
    "SkillAccessDenied",
    "SkillIndex",
    "SkillScopeStores",
    "SkillStorageBackend",
    "SkillStorageService",
    "SkillSyncResult",
    "assert_access",
    "new_scope_backend",
    "new_skill_backend",
    "workspace_access_checker",
]
