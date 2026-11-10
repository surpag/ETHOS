"""sandbox 服务：把用户会话绑定到唯一 E2B Sandbox。

对应 Go 版 internal/sandbox/service.go。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
from e2b import ALL_TRAFFIC, AsyncSandbox
from e2b.sandbox.sandbox_api import SandboxLifecycle

from ..skills.types import SCOPE_PROJECT, SCOPE_USER
from ..volume.service import Service as VolumeService
from ..volume.types import CUSTOM_SKILLS_PATH
from ..workspace.layout import WORKSPACE
from ..workspace.store import WorkspaceStore
from ..workspace.volume_backend import LocalVolumeBackend
from ..workspace.volume_service import WorkspaceVolumeService

if TYPE_CHECKING:  # 仅类型标注：运行期由 main.py 注入实例
    from ..skills.storage import SkillScopeStores, SkillStorageService

CONNECT_TIMEOUT_MS = 60 * 10
logger = logging.getLogger(__name__)

# E2B 硬限制：每个沙箱最多放行 10 个域名，超出服务端直接 400 且不可提额
MAX_ALLOWED_DOMAINS = 10


def allowed_domains_from_env() -> list[str]:
    """环境变量兜底白名单（EDITH_SANDBOX_ALLOWED_DOMAINS，逗号/分号/空白分隔）。"""
    raw = os.getenv("EDITH_SANDBOX_ALLOWED_DOMAINS", "")
    return [d for d in re.split(r"[,;\s]+", raw) if d]


class SandboxNotFound(Exception):
    pass


class Service:
    def __init__(
        self,
        db: aiosqlite.Connection,
        template: str,
        volumes: VolumeService,
        workspaces: WorkspaceStore | None = None,
        workspace_volumes: WorkspaceVolumeService | None = None,
        skill_scopes: SkillScopeStores | None = None,
    ) -> None:
        self._db = db
        self._template = template
        self._volumes = volumes
        self._workspaces = workspaces
        self._workspace_volumes = workspace_volumes
        # Skill 存储的作用域分发点（用户级 / 项目级）。只收门面而不是两个门面：
        # 作用域的选择规则必须只有一处，否则「什么时候才算项目级」会在调用点各写一遍。
        self._scopes = skill_scopes
        self._skills = skill_scopes.user() if skill_scopes is not None else None
        # 需要在下一次拿到沙箱时补一次 pull 的会话。键里带 workspace_id 是必须的：
        # 回拉项目级要知道拉哪个工作区的权威副本，而「挂起时刻的工作区」与
        # 「兑现时刻请求里带的工作区」未必是同一个（会话可以换绑工作区）。
        self._skills_pending: set[tuple[str, str, str]] = set()
        # B3 快路径：{workspace_id}:{sandbox_id} → 本地目录指纹（未变化则跳过逐文件远端比对）
        self._b3_fingerprints: dict[str, tuple] = {}
        # 出口策略：{sandbox_id: (domains...)}，已下发过的策略（避免每次连接重复 update_network）
        self._net_policies: dict[str, tuple[str, ...]] = {}

    async def _resolve_allowed_domains(
        self, user_id: str, workspace_id: str
    ) -> list[str]:
        """解析出口白名单：workspace metadata.allowedDomains 优先，否则取环境变量默认。

        返回空列表 = 不限制（E2B 默认放通全部出口）。
        E2B 硬限制：最多 10 个域名，超出会被服务端 400 拒绝，这里先截断并告警。
        """
        domains: list[str] = []
        if self._workspaces is not None and workspace_id:
            try:
                record = await self._workspaces.get(user_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 - 查询失败不阻塞沙箱
                logger.warning("load workspace for network policy failed: %s", exc)
                record = None
            metadata = (record or {}).get("metadata") or {}
            raw = metadata.get("allowedDomains")
            if isinstance(raw, list):
                domains = [str(d).strip() for d in raw if str(d).strip()]
        if not domains:
            domains = allowed_domains_from_env()
        if len(domains) > MAX_ALLOWED_DOMAINS:
            logger.warning(
                "allowedDomains %d 个，超过 E2B 每沙箱上限 %d，已截断",
                len(domains),
                MAX_ALLOWED_DOMAINS,
            )
            domains = domains[:MAX_ALLOWED_DOMAINS]
        return domains

    async def _ensure_network_policy(
        self, sandbox: AsyncSandbox, user_id: str, workspace_id: str
    ) -> None:
        """把出口白名单下发到已存在的沙箱（用于"先建沙箱、后收紧策略"）。

        只在期望白名单非空时下发：update_network 是**整体替换**（省略字段会被清空），
        传空对象即可恢复全放通；这里不主动恢复，避免每次连接多一次往返。
        下发失败不阻塞连接，但必须显式告警（不能静默当作已收紧）。
        """
        domains = await self._resolve_allowed_domains(user_id, workspace_id)
        if not domains:
            return
        policy = tuple(domains)
        sandbox_id = getattr(sandbox, "sandbox_id", "")
        if self._net_policies.get(sandbox_id) == policy:
            return
        try:
            await sandbox.update_network(
                {"allow_out": list(domains), "deny_out": [ALL_TRAFFIC]}
            )
        except Exception as exc:  # noqa: BLE001 - 失败不阻塞，但不能装作已收紧
            logger.warning(
                "apply egress policy to sandbox %s failed: %s", sandbox_id, exc
            )
            return
        self._net_policies[sandbox_id] = policy
        logger.info(
            "sandbox %s egress restricted to %d domains: %s",
            sandbox_id,
            len(domains),
            domains,
        )

    async def _load_sandbox_id(self, user_id: str, session_id: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT sandbox_id FROM user_sandboxes WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["sandbox_id"] if row else None

    async def session_workspace_id(self, user_id: str, session_id: str) -> str:
        """会话绑定的工作区 id；普通对话（未选工作区）返回空串。

        路径校验需要它：`workspace/{id}/…` 与 `skills/project/{id}/…` 都只代表
        「当前会话的工作区」，缺 ID 段时要靠它补全，有 ID 段时要靠它判越权。
        读 HTTP 层（文件面板）没有别的途径知道这件事——它只拿得到 sessionId。
        """
        cursor = await self._db.execute(
            "SELECT workspace_id FROM session_meta WHERE user_id = ? AND session_id = ?",
            (user_id.strip(), session_id.strip()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return (row["workspace_id"] or "") if row else ""

    async def existing_workspace(self, user_id: str, session_id: str) -> AsyncSandbox:
        """仅连接已经创建的 Sandbox，不会创建资源。

        只读场景（浏览/预览/下载）使用；前端文件面板已走本地直读（P0），
        此处不再同步工作区文件。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        sandbox_id = await self._load_sandbox_id(user_id, session_id)
        if sandbox_id is None:
            raise SandboxNotFound("sandbox mapping not found")
        workspace = await AsyncSandbox.connect(sandbox_id, timeout=CONNECT_TIMEOUT_MS)
        return workspace

    # 本次新增（P0 性能优化）：Local 后端下把 workspace/ 相对路径映射到本地磁盘
    async def local_workspace_path(self, user_id: str, relative: str):
        """返回 workspace/{workspace_id}/... 对应的本地磁盘路径；非 Local 后端或不存在返回 None。

        命中后 HTTP 层直接 os.scandir/read_bytes 本地直读，毫秒级，完全绕开 E2B 网络往返。
        """
        if self._workspace_volumes is None:
            return None
        backend = self._workspace_volumes.backend
        if not isinstance(backend, LocalVolumeBackend):
            return None
        # 归属校验：该 workspace 必须属于当前用户，防越权
        parts = (relative or "").strip().replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] == "workspace" and self._workspaces is not None:
            workspace_id = parts[1]
            record = await self._workspaces.get(user_id, workspace_id)
            if record is None:
                return None
            # 方案 B（B1a）：确保该 workspace 的 local_root 已注册（惰性、带缓存）
            await self._workspace_volumes.load_and_bind_local_root(
                user_id, workspace_id
            )
        return backend.resolve_workspace_path(relative)

    async def write_local_workspace_file(
        self, user_id: str, relative: str, content: str
    ) -> bool:
        """把文件内容写回本地工作区源（Agent 修改落地）。

        仅 Local 后端且路径位于 workspace/{id}/ 下时生效；其余（E2B 后端 / uploads 等
        沙箱独有目录）返回 False，调用方静默跳过。用于 sandbox_write_file 的双写。
        """
        local_path = await self.local_workspace_path(user_id, relative)
        if local_path is None or local_path.is_dir():
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content, encoding="utf-8")
        return True

    # ---- 方案 B（B2/B3）：与本地目录增量同步 ----

    async def sync_sandbox_to_local(
        self, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> int:
        """B2：把沙箱中与本地不同的文件拉回本地目录（覆盖 run_command 写文件盲区）。"""
        if self._workspace_volumes is None or not workspace_id:
            return 0
        local_root = await self._workspace_volumes.load_and_bind_local_root(
            user_id, workspace_id
        )
        if not local_root:
            return 0
        from ..workspace.sync import sync_sandbox_to_local as _sync

        try:
            return await _sync(
                sandbox,
                workspace_id,
                Path(local_root),
                prefix=self._workspace_volumes.local_root_prefix_of(workspace_id),
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用
            logger.warning("B2 sync failed ws=%s: %s", workspace_id, exc)
            return 0

    async def sync_local_to_sandbox(
        self, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> int:
        """B3：把本地新增/修改的文件推送到沙箱（工具调用前 / 用户发消息时）。"""
        if self._workspace_volumes is None or not workspace_id:
            return 0
        local_root = await self._workspace_volumes.load_and_bind_local_root(
            user_id, workspace_id
        )
        if not local_root:
            return 0
        from ..workspace.sync import scan_local_fingerprint
        from ..workspace.sync import sync_local_to_sandbox as _sync

        root = Path(local_root)
        # 廉价快路径：本地自上次同步（同一沙箱）以来未变化 → 跳过逐文件远端比对
        # 键含 sandbox_id：沙箱重建（新 sandbox_id）时必然重新全量同步，不会误跳过
        key = f"{workspace_id}:{getattr(sandbox, 'sandbox_id', '')}"
        fingerprint: tuple | None = None
        try:
            fingerprint = scan_local_fingerprint(root)
            if self._b3_fingerprints.get(key) == fingerprint:
                return 0
        except Exception as exc:  # noqa: BLE001 - 指纹失败不影响正常同步
            logger.warning("B3 fingerprint failed ws=%s: %s", workspace_id, exc)

        try:
            count = await _sync(
                sandbox,
                workspace_id,
                root,
                prefix=self._workspace_volumes.local_root_prefix_of(workspace_id),
            )
            if fingerprint is not None:
                self._b3_fingerprints[key] = fingerprint
            return count
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用
            logger.warning("B3 sync failed ws=%s: %s", workspace_id, exc)
            return 0

    # ---- Skill：权威副本 ↔ 沙箱运行副本（按作用域分发） ----

    @property
    def skills(self) -> SkillStorageService | None:
        """用户级 Skill 存储门面（未配置时为 None，全部钩子自动跳过）。"""
        return self._skills

    def _scope_service(
        self, scope: str, workspace_id: str
    ) -> SkillStorageService | None:
        """取某个作用域的存储门面；该作用域在本上下文不成立时返回 None。

        「不成立」只有两种，都不是错误：
        - 没有注入作用域存储（测试 / 精简部署）；
        - 项目级没有 workspace 上下文（纯聊天会话本来就只有用户级 Skill）。

        项目级**结构性不可用**时（`e2b` 后端只支持按用户挂载）也返回 None：
        那种后端每次调用都返回 `unavailable` 而不抛错，接在这里等于给每次工具调用
        加一次空跑 + 一行日志。不可用的原因由 resolver 的 `ScopeReport` 上报，
        同步钩子不该成为第二个（更吵的）真相来源。
        """
        if self._scopes is None:
            return None
        if scope == SCOPE_USER:
            return self._scopes.user()
        if scope != SCOPE_PROJECT:
            return None
        key = (workspace_id or "").strip()
        if not key or not self._scopes.project_supported():
            return None
        return self._scopes.project(key)

    async def _push_scope(
        self, scope: str, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> None:
        """把一个作用域的权威副本推到沙箱（后端内部走指纹快路径）。

        失败只告警：Skill 缺一个文件不该让整次工具调用失败；状态与原因由存储门面
        写日志，不会静默。作用域之间**各自独立 try**——两者目录与状态文件都相互
        独立，共用一个 try 会让「项目级出问题」变成「用户级也不推」。
        """
        service = self._scope_service(scope, workspace_id)
        if service is None:
            return
        try:
            await service.push(user_id, sandbox)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "push %s skills failed user=%s ws=%s error=%s",
                scope,
                user_id[:12],
                workspace_id[:12],
                exc,
            )

    async def _push_skills(
        self, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> None:
        """把用户级（以及有工作区上下文时的项目级）权威副本推到沙箱。"""
        await self._push_scope(SCOPE_USER, user_id, workspace_id, sandbox)
        if (workspace_id or "").strip():
            await self._push_scope(SCOPE_PROJECT, user_id, workspace_id, sandbox)

    async def _pull_scope(
        self, scope: str, user_id: str, workspace_id: str, sandbox: AsyncSandbox
    ) -> None:
        service = self._scope_service(scope, workspace_id)
        if service is None:
            return
        try:
            await service.pull(user_id, sandbox)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pull %s skills failed user=%s ws=%s error=%s",
                scope,
                user_id[:12],
                workspace_id[:12],
                exc,
            )

    async def pull_skills(
        self,
        user_id: str,
        workspace_id: str,
        sandbox: AsyncSandbox,
        scopes: Collection[str] | None = None,
    ) -> None:
        """沙箱 → 权威副本（覆盖 run_command / 脚本落盘等 write_file 之外的写入）。

        `scopes` 是工具层判定「本次调用改了哪一层」的结果：命中就只拉命中的那层，
        避免一次写 `skills/custom` 顺带触发项目级的整棵树比对。None 表示全拉。
        """
        wanted = scopes if scopes is not None else (SCOPE_USER, SCOPE_PROJECT)
        for scope in (SCOPE_USER, SCOPE_PROJECT):
            if scope not in wanted:
                continue
            await self._pull_scope(scope, user_id, workspace_id, sandbox)

    def mark_skill_pull_pending(
        self, user_id: str, session_id: str, workspace_id: str = ""
    ) -> None:
        """登记「本会话有一次 Skill 写入尚未回拉」，下次拿沙箱时补上。

        记下**当时**的 workspace_id：会话可以换绑工作区，用兑现时刻的上下文去猜
        会拉错项目的权威副本。空 workspace 表示「只可能有用户级变更」。
        """
        if self._scopes is None:
            return
        self._skills_pending.add(
            (user_id, session_id, (workspace_id or "").strip())
        )

    def _pending_workspaces(self, user_id: str, session_id: str) -> list[str]:
        """该会话挂起的工作区列表（去重，保持插入顺序无关）。"""
        found = {
            ws
            for (uid, sid, ws) in self._skills_pending
            if uid == user_id and sid == session_id
        }
        return sorted(found)

    def _take_pending(self, user_id: str, session_id: str) -> list[str]:
        """摘掉该会话的挂起标记并返回需要回拉的工作区列表。

        先摘再拉：沙箱连不上时这一轮的内容只能留在沙箱里，但标记不能留着——
        留着会让下一次拿沙箱时重复回拉一次已经不再存在的写入。
        """
        found = self._pending_workspaces(user_id, session_id)
        for ws in found:
            self._skills_pending.discard((user_id, session_id, ws))
        return found

    async def _drain_skill_pending(
        self, user_id: str, session_id: str, sandbox: AsyncSandbox
    ) -> None:
        """兑现挂起的回拉。顺序：先拉（吸收沙箱改动）再推（本地权威覆盖）。"""
        for workspace_id in self._take_pending(user_id, session_id):
            await self.pull_skills(user_id, workspace_id, sandbox)

    async def flush_skill_sync(self, user_id: str, session_id: str) -> None:
        """run 收尾兜底：把本轮最后一次未回拉的 Skill 写入落地。

        这是「不依赖写类工具钩子」的机制保证——即使 Agent 用 `python x.py` 之类
        完全不含 skills 字样的命令改了 Skill，只要本轮用过沙箱，收尾时也会回拉一次。
        代价：一次 `existing_workspace` 连接（仅在该会话有挂起标记时发生）。

        不需要 `workspace_id` 参数：挂起标记里已经记了「当时是哪个工作区」。
        """
        if not self._pending_workspaces(user_id, session_id):
            return
        try:
            sandbox = await self.existing_workspace(user_id, session_id)
        except Exception as exc:  # noqa: BLE001 - 沙箱已被回收：本轮内容只能留在沙箱里
            self._take_pending(user_id, session_id)
            logger.warning(
                "flush skills skipped (sandbox unavailable) user=%s session=%s error=%s",
                user_id[:12],
                session_id[:12],
                exc,
            )
            return
        await self._drain_skill_pending(user_id, session_id, sandbox)

    async def _sync_workspace_files(
        self,
        workspace: AsyncSandbox,
        workspace_id: str = "",
        force: bool = False,
        user_id: str = "",
    ) -> None:
        """Local 后端：把指定工作区文件同步进沙箱（E2B 后端 no-op）。

        - 绑定本地目录的工作区：走**增量同步**（前缀 / 忽略规则 / 指纹快路径 / 逐文件写入），
          且失败只告警不抛出——本地目录是权威源、读取走直连，同步失败不应阻塞工具调用。
        - 未绑定的工作区：仅在沙箱新建（force）或该工作区有新写入（dirty）时做全量推送。

        全量推送失败（WorkspaceSyncError）上抛，让工具执行层感知「工作区文件尚未同步完成」，
        避免 Agent 拿到不完整的沙箱视图后盲目执行命令。
        """
        if self._workspace_volumes is None or not workspace_id:
            return
        bound = bool(
            user_id
            and await self._workspace_volumes.load_and_bind_local_root(
                user_id, workspace_id
            )
        )
        if bound:
            await self.sync_local_to_sandbox(user_id, workspace_id, workspace)
            return
        if force or self._workspace_volumes.is_dirty(workspace_id):
            await self._workspace_volumes.sync_to_sandbox(workspace, workspace_id)

    async def workspace(
        self, user_id: str, session_id: str, workspace_id: str = ""
    ) -> AsyncSandbox:
        """返回可恢复的 E2B Sandbox；首次调用才创建远端资源。

        workspace_id 用于按需同步（Local 后端）：仅同步当前会话所属工作区，
        且只在「沙箱新建」或「该工作区有新写入（dirty）」时同步，避免每次工具调用全量同步。
        """
        user_id, session_id = user_id.strip(), session_id.strip()
        if not user_id or not session_id:
            raise ValueError("sandbox requires a user ID and session ID")
        sandbox_id = await self._load_sandbox_id(user_id, session_id)
        if sandbox_id is not None:
            workspace = await AsyncSandbox.connect(
                sandbox_id, timeout=CONNECT_TIMEOUT_MS
            )
            await self._db.execute(
                "UPDATE user_sandboxes SET updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            await self._db.commit()
            # 出口策略：先建沙箱、后收紧的场景在这里补发（带 memo，不重复下发）
            await self._ensure_network_policy(workspace, user_id, workspace_id)
            await self._sync_workspace_files(workspace, workspace_id, user_id=user_id)
            # Skill：先兑现上轮挂起的回拉，再把权威副本推到沙箱。
            # 必须独立于 workspace_id —— 纯聊天会话（无工作区）同样要能创建/读取用户级 Skill；
            # 项目级只在有工作区时参与，由 `_push_skills` 内部判定。
            await self._drain_skill_pending(user_id, session_id, workspace)
            await self._push_skills(user_id, workspace_id, workspace)
            return workspace

        # volume_mounts: key = 挂载路径，value = Volume 名称/对象。
        # 与 SDK 签名 Dict[path, Union[AsyncVolume, str]] 一致（SDK 内部 name=vol.name）。
        volume_mounts: dict[str, str] = {}

        # 1. 用户 Skills：存储后端决定「挂载」还是「稍后同步」
        #    - E2B 后端：返回 MountSpec，挂到 /home/user/skills/custom
        #    - Local 后端：返回 None（沙箱目录随后由 push 填充）
        skill_mount = (
            await self._skills.mount_spec(user_id) if self._skills is not None else None
        )
        if skill_mount is not None:
            volume_mounts[skill_mount.path] = skill_mount.name
        elif self._skills is None:
            # 兼容路径：没有注入存储门面时退回旧的「直接问 Volume 服务」
            user_mount = await self._volumes.mount_for_user(user_id)
            if user_mount is not None:
                volume_mounts[CUSTOM_SKILLS_PATH] = user_mount.name

        # 2. 确保当前工作区有持久化 Volume（懒创建，否则 git clone 会落到沙箱临时盘、跨会话丢失），
        #    再挂载该用户全部已建卷的工作区。
        if self._workspace_volumes is not None and workspace_id:
            try:
                await self._workspace_volumes.ensure_volume(user_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 - 卷创建失败不阻塞沙箱，但该工作区文件不持久
                logger.warning(
                    "ensure workspace volume failed ws=%s: %s", workspace_id, exc
                )
        if self._workspaces is not None:
            try:
                ws_entries = await self._workspaces.list_mounts(user_id)
            except Exception as exc:  # noqa: BLE001 - 工作区查询失败不阻塞沙箱
                logger.warning("failed to load workspace mounts: %s", exc)
                ws_entries = []
            for volume_name, ws_id in ws_entries:
                volume_mounts[WORKSPACE.workspace_path(ws_id)] = volume_name

        # 创建沙箱
        allowed_domains = await self._resolve_allowed_domains(user_id, workspace_id)
        workspace = await AsyncSandbox.create(
            template=self._template,
            timeout=CONNECT_TIMEOUT_MS,
            metadata={"edith_user_id": user_id, "edith_session_id": session_id},
            lifecycle=SandboxLifecycle(on_timeout="pause", auto_resume=True),
            volume_mounts=volume_mounts,
            # 出口白名单（默认放通；配置了 allowedDomains 才收紧）
            network=(
                {"allow_out": list(allowed_domains), "deny_out": [ALL_TRAFFIC]}
                if allowed_domains
                else None
            ),
        )
        if allowed_domains:
            sandbox_id = getattr(workspace, "sandbox_id", "")
            self._net_policies[sandbox_id] = tuple(allowed_domains)
            logger.info(
                "sandbox %s created with egress allowlist (%d domains): %s",
                sandbox_id,
                len(allowed_domains),
                allowed_domains,
            )
        # Local 后端：沙箱创建后把当前工作区文件同步进沙箱（E2B 后端 no-op）
        await self._sync_workspace_files(
            workspace, workspace_id, force=True, user_id=user_id
        )
        # 新沙箱是空盘：把 Skill 全量推一遍（Local 后端靠这一步跨会话生效）
        await self._push_skills(user_id, workspace_id, workspace)
        await self._db.execute(
            "INSERT INTO user_sandboxes (user_id, session_id, sandbox_id) VALUES (?, ?, ?)",
            (user_id, session_id, workspace.sandbox_id),
        )
        await self._db.commit()
        return workspace
