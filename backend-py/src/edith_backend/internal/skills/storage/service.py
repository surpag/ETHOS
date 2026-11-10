"""用户 Skill 存储门面：上层唯一入口，屏蔽后端差异。

职责
----
1. **后端不可见**：`catalog` / `sandbox` / agentrun 只拿到本类，拿不到
   `LocalSkillBackend` 或 `E2BSkillBackend`，因此切换后端不需要改业务代码。
2. **并发收敛**：按 user_id 串行化「比对 + 写盘 + 更新状态」临界区。
   取舍说明：Skill 目录常态 < 20 个 KB 级文件，临界区是毫秒级本地 IO；
   而 workspace 之所以用 `WorkspaceLanes` 做整会话互斥，是因为两个沙箱会互相
   覆盖整棵镜像树（量级完全不同）。把 Skill 同步升级成会话级租约，会让「改一个
   SKILL.md」变成「整个会话被 409 拒绝」，代价远大于收益。
   已知上限与 `SessionLanes` 相同：进程内锁，多副本部署时不跨进程生效。
3. **可观测**：每次同步返回 `SkillSyncResult(status, reason)`，失败不再被
   压成「空字符串」——「用户没有 Skill」与「存储不可用」必须能区分。
4. **快路径**：本地副本未变化（廉价指纹一致）时跳过远端比对，
   避免每次工具调用都发起一次 `files.list`。
"""

from __future__ import annotations

import asyncio
import logging

from .base import (
    STATUS_OK,
    STATUS_UNAVAILABLE,
    MountSpec,
    SkillIndex,
    SkillStorageBackend,
    SkillSyncResult,
)

logger = logging.getLogger(__name__)


def _short(user_id: str) -> str:
    """日志里用短指纹，避免把完整 user id 铺到日志里。"""
    return user_id[:12] if user_id else "?"


class SkillStorageService:
    """用户 Skill 存储的门面（单一入口）。"""

    def __init__(self, backend: SkillStorageBackend) -> None:
        self._backend = backend
        self._locks: dict[str, asyncio.Lock] = {}
        # { (user_id, sandbox_id): 本地副本指纹 }：只有本地后端会填
        self._fingerprints: dict[tuple[str, str], tuple] = {}
        # 最近一次索引读取结果：供诊断接口/日志在同步之外看到「为什么是空的」
        self._last_index: dict[str, SkillIndex] = {}

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def _lock(self, user_id: str) -> asyncio.Lock:
        # 单事件循环内的惰性建锁：不需要额外加锁
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def ensure(self, user_id: str) -> None:
        try:
            await self._backend.ensure(user_id)
        except Exception as exc:  # noqa: BLE001 - 懒创建失败交给后续读取报告
            logger.warning("ensure user skill storage failed user=%s error=%s", _short(user_id), exc)

    async def mount_spec(self, user_id: str) -> MountSpec | None:
        try:
            return await self._backend.mount_spec(user_id)
        except Exception as exc:  # noqa: BLE001 - 挂载失败不阻塞沙箱创建
            logger.warning("mount user skill storage failed user=%s error=%s", _short(user_id), exc)
            return None

    async def read_index(self, user_id: str) -> SkillIndex:
        """读取用户 Skill 索引（后端负责自愈）；失败返回 unavailable 而非空串。"""
        async with self._lock(user_id):
            index = await self._backend.read_index(user_id)
        self._last_index[user_id] = index
        if index.status == STATUS_UNAVAILABLE:
            # 存储不可用必须留痕：这条故障以前只能靠翻后端终端
            logger.warning(
                "user skill index unavailable user=%s backend=%s reason=%s",
                _short(user_id),
                self._backend.name,
                index.reason,
            )
        return index

    async def last_index(self, user_id: str) -> SkillIndex | None:
        return self._last_index.get(user_id)

    async def push(self, user_id: str, sandbox) -> SkillSyncResult:
        """权威副本 → 沙箱。本地副本未变化时走快路径（不发起远端比对）。"""
        key = (user_id, getattr(sandbox, "sandbox_id", "") or "")
        fingerprint = self._backend.local_fingerprint(user_id)
        if fingerprint is not None and self._fingerprints.get(key) == fingerprint:
            return SkillSyncResult(status=STATUS_OK, skipped=True, reason="unchanged")

        async with self._lock(user_id):
            result = await self._backend.push(user_id, sandbox)
        if result.status in (STATUS_OK, "empty") and fingerprint is not None:
            self._fingerprints[key] = fingerprint
        self._log("push", user_id, result)
        return result

    async def pull(self, user_id: str, sandbox) -> SkillSyncResult:
        """沙箱 → 权威副本（覆盖 write_file 之外的写入途径）。"""
        async with self._lock(user_id):
            result = await self._backend.pull(user_id, sandbox)
        key = (user_id, getattr(sandbox, "sandbox_id", "") or "")
        fingerprint = self._backend.local_fingerprint(user_id)
        if result.status == STATUS_OK and fingerprint is not None:
            # 刚拉过：本地与这个沙箱一致，下次 push 可以跳过一次远端比对
            self._fingerprints[key] = fingerprint
        else:
            # 失败/部分失败：清掉快路径，确保下一轮一定重试
            self._fingerprints.pop(key, None)
        self._log("pull", user_id, result)
        return result

    @staticmethod
    def _log(action: str, user_id: str, result: SkillSyncResult) -> None:
        if result.skipped and result.status == STATUS_OK:
            logger.debug("skill %s skipped user=%s reason=%s", action, _short(user_id), result.reason)
            return
        log = logger.warning if result.status == STATUS_UNAVAILABLE else logger.info
        log(
            "skill %s user=%s status=%s pushed=%d pulled=%d deleted=%d reason=%s",
            action,
            _short(user_id),
            result.status,
            result.pushed,
            result.pulled,
            result.deleted,
            result.reason,
        )
