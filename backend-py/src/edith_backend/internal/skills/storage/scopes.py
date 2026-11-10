"""按作用域选后端：用户级 / 项目级，并为项目级按 workspace 缓存门面。

为什么需要「按 workspace 缓存门面」而不是每次 new 一个
------------------------------------------------------
`SkillStorageService` 持有三份进程内状态，全都是**跨调用累积**才有意义的东西：

- `_fingerprints`：本地未变时跳过远端比对（不然每次工具调用都要 `files.list`）；
- 后端的 `_baselines`：判断「本地在与这个沙箱上次同步之后又被改过」，冲突快照靠它；
- 后端的 `_ensured_dirs`：每个沙箱只建一次目录。

每次请求都新建实例，这三样全部归零：快路径失效、冲突快照不再产生（**会静默少一份
副本**）、每轮多一次远端往返。所以项目级门面必须按 `workspace_id` 缓存。

与既有实现一致的上限：进程内缓存，多副本部署时各自独立（`SessionLanes` /
`WorkspaceLanes` / `RunPlanRegistry` 都是这个性质）。

项目级为什么首期只支持 local
----------------------------
`e2b` 后端的 `mount_spec(user_id)` 只能挂**一个用户卷**，项目级要按
`(user_id, workspace_id)` 挂载；而 E2B Volumes 目前是 private beta（`POST /volumes`
恒 403 `use of volumes is not enabled`）。因此 `e2b` 下的项目层返回
`status=unavailable, reason="project skills require local backend"`，
而不是假装「这个项目没有 Skill」——后者会让人一直查错地方。
"""

from __future__ import annotations

import logging

from ..types import SCOPE_PROJECT, SCOPE_USER
from .access import AccessChecker
from .base import (
    STATUS_UNAVAILABLE,
    MountSpec,
    SkillIndex,
    SkillStorageBackend,
    SkillSyncResult,
)
from .service import SkillStorageService

logger = logging.getLogger(__name__)


class _UnavailableBackend(SkillStorageBackend):
    """明确表示「这个作用域在当前配置下不可用」，每次调用都带原因返回。"""

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def mount_spec(self, user_id: str) -> MountSpec | None:
        return None

    async def read_index(self, user_id: str) -> SkillIndex:
        return SkillIndex(status=STATUS_UNAVAILABLE, reason=self._reason)

    async def push(self, user_id: str, sandbox) -> SkillSyncResult:
        return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=self._reason)

    async def pull(self, user_id: str, sandbox) -> SkillSyncResult:
        return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=self._reason)


def new_scope_backend(
    scope: str,
    kind: str,
    workspace_id: str | None = None,
    volumes=None,
    access_checker: AccessChecker | None = None,
) -> SkillStorageBackend:
    """按作用域构造后端。

    用户级走既有工厂（行为不变）；项目级要求 `local`，其余组合给出可读的不可用原因。
    `local` / `project` 在这里延迟导入：`local` 依赖 `skills.catalog`，而
    `skills.catalog` 又引用本包的 `SkillIndex`，模块级导入会成环（见本包 `__init__`）。
    """
    if scope == SCOPE_USER:
        from . import new_skill_backend

        return new_skill_backend(kind, volumes)

    if scope != SCOPE_PROJECT:
        # 拼错的作用域不能降级成用户级：那会让「项目 Skill 不见了」变成长期悬案
        return _UnavailableBackend(f"unknown skill scope: {scope!r}")

    normalized = (kind or "").strip().lower()
    if normalized != "local":
        return _UnavailableBackend("project skills require local backend")

    from .project import ProjectSkillBackend

    return ProjectSkillBackend(
        workspace_id or "", access_checker=access_checker
    )


class SkillScopeStores:
    """作用域 → 存储门面的唯一分发点。

    用户级门面由组合根传入（`main.py` 已经构造好并交给 `Catalog` / `SandboxService`），
    这里只额外管理项目级的按 workspace 缓存。

    `access_checker` 是项目级的归属判定器（`access.workspace_access_checker(store)`）。
    不传时项目级 **fail-closed**：读返回 `unavailable`、写拒绝且不建目录。
    宁可「项目 Skill 功能不可用」，也不能「所有项目对所有用户可见」。
    """

    def __init__(
        self,
        kind: str,
        user_service: SkillStorageService | None = None,
        volumes=None,
        access_checker: AccessChecker | None = None,
    ) -> None:
        self._kind = (kind or "").strip().lower() or "local"
        self._volumes = volumes
        self._access_checker = access_checker
        if user_service is None:
            from . import new_skill_backend

            user_service = SkillStorageService(new_skill_backend(self._kind, volumes))
        self._user = user_service
        self._project: dict[str, SkillStorageService] = {}

    @property
    def kind(self) -> str:
        return self._kind

    def project_supported(self) -> bool:
        """项目级是否需要真的尝试同步。

        `e2b` 下项目级是 `_UnavailableBackend`：它每次都返回「不可用」而不是抛错，
        于是调用点无法用「失败」来判断该不该继续——每轮工具调用都会白跑一次并留一行
        日志。同步钩子据此跳过（这是**配置结论**，不是运行时降级）；「为什么不可用」
        由 resolver 的 `ScopeReport(status=unavailable, reason=...)` 上报，那才是
        模型与前端该看到的入口。
        """
        return self._kind == "local"

    def user(self) -> SkillStorageService:
        return self._user

    def project(self, workspace_id: str) -> SkillStorageService:
        """取（必要时创建）某个 workspace 的项目级门面。

        空 workspace 抛 `ValueError` 而不是返回「空存储」：调用方在没有项目上下文时
        根本不该问项目级要东西，静默返回空会把调用方 bug 伪装成「这个项目没有 Skill」。
        """
        key = (workspace_id or "").strip()
        if not key:
            raise ValueError("project scope requires a non-empty workspace_id")
        service = self._project.get(key)
        if service is None:
            service = SkillStorageService(
                new_scope_backend(
                    SCOPE_PROJECT, self._kind, key, self._volumes, self._access_checker
                )
            )
            self._project[key] = service
        return service
