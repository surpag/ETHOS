"""用户 Skill 存储的后端抽象。

设计背景
--------
用户 Skill（`skills/custom/<name>/SKILL.md`）原本只能落在 E2B Volume 上，而
Volumes 在 E2B 侧是 private beta（未开通时 `POST /volumes` 恒 403），导致整个
「用户级 Skill」功能空转：卷不存在 → 挂载失败 → 沙箱销毁即丢 → 索引恒为空。

因此把「用户 Skill 存放在哪」抽成后端，与 workspace 已经跑通的
`workspace/volume_backend.py` 同构：

- `LocalSkillBackend`：权威副本 = 宿主目录 `user-skills/{user_id}/`，
  沙箱 `/home/user/skills/custom` 是运行副本，push/pull 双向同步。
- `E2BSkillBackend`：权威副本 = E2B Volume，挂载即共享，push/pull 为 no-op。

上层（`skills/catalog.py`、`sandbox/service.py`、agentrun）只依赖
`SkillStorageService` 门面，拿不到后端类型，因此切换后端不需要改 Agent 逻辑。

路径约定（与 sync_overview.py / CUSTOM_PATH 保持单一来源）
--------------------------------------------------------
沙箱内：`/home/user` + `skills/custom` = `SANDBOX_SKILLS_PATH`
索引：  `skills/custom/overview.md`（`sync_overview.py` 写的就是这个位置）
"""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ...workspace.layout import WORKSPACE
from ..types import CUSTOM_PATH, PROJECT_PATH, SkillSummary

# 状态取值：区分「用户没有 Skill」与「存储不可用」——两者注入文本都是空，
# 但只有后者是故障，必须能被 trace / HTTP / 日志区分出来。
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"

# 沙箱内用户 Skill 目录的绝对路径（/home/user/skills/custom）
SANDBOX_SKILLS_PATH = posixpath.join(WORKSPACE.root, CUSTOM_PATH)
# 沙箱内项目级 Skill 的父目录；具体目录还需拼一层 workspace_id
SANDBOX_PROJECT_ROOT = posixpath.join(WORKSPACE.root, PROJECT_PATH)
# 索引文件在 Skill 根目录内的相对名（与 sync_overview.py 一致）
OVERVIEW_FILENAME = "overview.md"


@dataclass(frozen=True)
class MountSpec:
    """沙箱创建时需要挂载存储的声明；无挂载语义的后端返回 None。"""

    name: str
    path: str = SANDBOX_SKILLS_PATH


@dataclass(frozen=True)
class SkillIndex:
    """一次「用户 Skill 索引读取」的完整结果。

    text 与旧实现（读 overview.md 原文）保持同一形态，注入侧零变化；
    status / reason / healed 是新增的可观测字段。
    """

    text: str = ""
    summaries: list[SkillSummary] = field(default_factory=list)
    status: str = STATUS_EMPTY
    reason: str = ""
    healed: bool = False


@dataclass(frozen=True)
class SkillSyncResult:
    """一次 push/pull 的结果；失败绝不静默（status + reason 必须可上报）。"""

    pushed: int = 0
    pulled: int = 0
    deleted: int = 0
    conflicts: list[str] = field(default_factory=list)
    #: 校验未通过、被移进 `.rejected/<ts>/` 的候选版本（相对权威根）
    rejected: list[str] = field(default_factory=list)
    status: str = STATUS_OK
    reason: str = ""
    skipped: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.pushed or self.pulled or self.deleted)


class SkillStorageBackend(ABC):
    """用户 Skill 存储的抽象边界（按 user_id 隔离，与 workspace 粒度不同）。"""

    #: 后端标识（local / e2b），只用于日志与诊断
    name: str = ""

    async def ensure(self, user_id: str) -> None:
        """确保该用户的存储存在（懒创建）。失败应抛出，由门面转成 status。"""
        return

    async def assert_access(self, user_id: str) -> None:
        """校验该 user 是否有权访问本作用域的存储（设计稿 7.5）。

        默认空实现：用户级作用域的隔离维度就是 `user_id` 本身，没有第三者可越权。
        项目级覆盖它，且必须 **fail-closed**（无权时抛 `SkillAccessDenied`）。
        放在基类而不是只写在项目级，是为了让「所有作用域操作先过它」成为结构性的
        事实——新增后端时不会因为忘了写而缺少这一层。
        """
        return

    @abstractmethod
    async def mount_spec(self, user_id: str) -> MountSpec | None:
        """返回沙箱挂载声明；本地后端返回 None（靠同步而非挂载）。"""

    @abstractmethod
    async def read_index(self, user_id: str) -> SkillIndex:
        """读取（并自愈）用户 Skill 索引。失败必须返回 unavailable，不得抛。"""

    async def push(self, user_id: str, sandbox) -> SkillSyncResult:
        """权威副本 → 沙箱运行副本；挂载型后端无需实现。"""
        return SkillSyncResult(status=STATUS_OK, skipped=True, reason="mount shared")

    async def pull(self, user_id: str, sandbox) -> SkillSyncResult:
        """沙箱运行副本 → 权威副本；挂载型后端无需实现。"""
        return SkillSyncResult(status=STATUS_OK, skipped=True, reason="mount shared")

    def local_fingerprint(self, user_id: str) -> tuple | None:
        """本地副本的廉价指纹；无本地副本的后端返回 None（调用方跳过快路径）。"""
        return None
