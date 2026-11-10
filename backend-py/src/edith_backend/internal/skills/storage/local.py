"""本地文件系统实现：宿主目录是权威副本，沙箱目录是运行副本。

为什么权威副本在宿主
--------------------
E2B Volumes 在 private beta 阶段未开通（`POST /volumes` 恒 403），把「用户级
Skill」押在它上面等于功能不可用。本地实现的语义与 `LocalVolumeBackend` 一致：
宿主目录持久、可靠、零网络往返；沙箱目录随建随同步。

同步复用的东西（不另造轮子）
----------------------------
`workspace/sync.py` 里已经有一整套经过线上事故打磨的机制：免下载比对
（`files.list` 的 size 对得上就不读字节）、单文件体积上限、二进制排除、
连续失败熔断、显式短超时、以及「两侧共识集合」的删除传播 + 五道护栏。
本模块只做两件事：
  1. 把 `mount` 参数化（Skill 的挂载根是 `/home/user/skills/custom`，
     不是 `workspace/{id}`），复用同一套同步；
  2. 把 `overview.md` 从「模型自觉维护的权威源」降级成「服务端派生的缓存」，
     读取时以目录为准自愈重建（见 `read_index`）。

关于 overview.md 不进同步
-------------------------
它由服务端持有（目录扫描结果），沙箱里的副本没有读者。因此本地根目录写一份
`.edithignore`（内容含 `overview.md` 与 `.edithignore` 自身），复用
`sync.is_ignored` 把它排除在两个方向之外，避免「拉回来一份旧的、再自愈写回、
下次推送又不一样」的来回抖动。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import re
import shutil
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from ...workspace.layout import WORKSPACE
from ...workspace.sync import (
    is_ignored,
    load_synced_paths,
    save_synced_paths,
    scan_local_fingerprint,
)
from ...workspace.sync import sync_local_to_sandbox as _push_impl
from ...workspace.sync import sync_sandbox_to_local as _pull_impl
from ..catalog import _load_skill
from ..types import CUSTOM_PATH, SkillSummary
from . import transaction
from .access import SkillAccessDenied
from .base import (
    OVERVIEW_FILENAME,
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
from .validate import SKILL_DIR_MAX_BYTES, platform_skill_names, validate_skill_tree

logger = logging.getLogger(__name__)

# 本地用户 Skill 根目录（环境变量可覆盖；与 workspace-volumes 同级、独立目录）
_DEFAULT_ROOT = "user-skills"

# 同步状态（删除传播的共识集合）文件名前缀：落在 workspace-sync/ 下，
# 绝不能放进 Skill 目录本身——那会被 Agent 看到、也会被 sync_overview.py 扫到。
_STATE_PREFIX = "skills-"

# 不参与同步的路径（服务端派生 / 冲突留存 / 替换事务的中间态）
# 冲突留存目录：pull 覆盖本地前，若本地自上次同步后被改过，先把旧版本整份存这里。
# 以 "." 开头 + 写进 .edithignore，保证既不被扫成 Skill，也不会同步进沙箱。
CONFLICT_DIR = ".conflicts"
_IGNORE_FILE = ".edithignore"
# 尾部通配（`is_ignored` 的 `前缀*` 形态）：暂存与旧版目录名带时间戳，写不成固定名。
# 漏掉它们会让暂存目录整个被 push 进沙箱——`_scan` 会跳过 "." 开头的目录，
# 但**同步不受 `_scan` 约束**，模型会在 skills/custom 下看到一个装满半成品的目录。
_IGNORE_CONTENT = (
    f"{OVERVIEW_FILENAME}\n"
    f"{_IGNORE_FILE}\n"
    f"{CONFLICT_DIR}\n"
    f"{transaction.REJECTED_DIR}\n"
    f"{transaction.STAGING_PREFIX}*\n"
    f"{transaction.OLD_PREFIX}*\n"
)

_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


def _safe_segment(raw: str) -> str:
    """把 user_id 变成安全的单段路径/文件名。

    Clerk user id 形如 `user_3Hm1...`，本身是安全的；这里做兜底是因为它最终会
    拼进文件系统路径：任何分隔符或 `..` 都必须被消除，否则等于允许越目录写入。
    一旦发生替换，追加内容摘要避免「两个不同用户撞成同一目录」。
    """
    text = (raw or "").strip()
    segment = _UNSAFE_SEGMENT.sub("_", text)
    if not segment or segment in {".", ".."}:
        raise ValueError("user id cannot be mapped to a safe directory name")
    if segment != text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        segment = f"{segment}-{digest}"
    return segment


#: 所有作用域根的父目录（`/home/user/skills`）；模板只建了它的两个子目录
_SKILLS_BASE = posixpath.dirname(SANDBOX_SKILLS_PATH)


def sandbox_mkdir_chain(root: str) -> list[str]:
    """`root` 及其在 `skills/` 下的各级父目录，由浅到深（幂等建目录用）。

    起点固定在 `skills/`：`/home/user` 是模板建的，再往上与 Skill 无关。
    非标准根（不在 `skills/` 下）只返回它自己——那是部署方自己的路径约定，
    替它造父目录属于越权猜测。
    """
    normalized = (root or "").rstrip("/")
    if not normalized:
        return []
    if normalized == _SKILLS_BASE or not normalized.startswith(_SKILLS_BASE + "/"):
        return [normalized]
    rest = normalized[len(_SKILLS_BASE) + 1 :].split("/")
    return [_SKILLS_BASE] + [
        f"{_SKILLS_BASE}/{'/'.join(rest[:index])}" for index in range(1, len(rest) + 1)
    ]


class ScannedSkill(NamedTuple):
    """一次目录扫描得到的条目。

    `dir_name` 与 `name` 分开是必须的：索引的**显示名**取 frontmatter 的 `name`，
    而**路径**必须用真实目录名。两者不一致时（人工改过 frontmatter、或从别处
    拷来的 Skill），只用一个名字会让索引写出一个不存在的路径，模型据此读文件
    必然失败——而失败方式是「读到空的 Skill」，不是报错。
    """

    dir_name: str
    name: str
    description: str


def render_overview(
    entries: list[ScannedSkill], base_path: str = CUSTOM_PATH
) -> str:
    """按 `sync_overview.py` 的**完全相同**格式生成索引文本。

    保持逐字节一致是刻意的：注入到 System Instruction 的形态不变，
    discovery 语义就不会因为「换了个生成方」而漂移。

    `base_path` 让同一份渲染同时服务两个作用域（用户级 `skills/custom`、
    项目级 `skills/project/{workspace_id}`），格式本身只有一份。
    """
    lines = [
        "# 用户 Skills 总览",
        "",
        "以下内容由脚本生成，请勿手动修改。",
        "",
    ]
    if not entries:
        lines.append("当前没有用户 Skills。")
    else:
        for entry in sorted(entries, key=lambda item: item.name):
            lines.append(f"- `{entry.name}`：{entry.description}")
            lines.append(f"  - 路径：`{base_path}/{entry.dir_name}/SKILL.md`")
    return "\n".join(lines) + "\n"


def _atomic_write_text(target: Path, text: str) -> None:
    """同目录写临时文件再 `os.replace`——读方永不会看到半个文件。

    `workspace/sync.py` 的本地写入是直接 `write_bytes`；Skill 索引是每轮 run
    都要读的东西，这里不沿用它，改用原子替换。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


class LocalSkillBackend(SkillStorageBackend):
    """用户 Skill 的本地副本后端（宿主目录为权威源）。

    三个参数化点让项目级后端可以**继承**而不是复制这整套同步逻辑：
    `root`（权威副本根）、`sandbox_root`（沙箱运行副本根）、`state_prefix`
    （删除传播共识集合的文件名前缀）。项目级只需要换这三个值。
    """

    name = "local"

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        sandbox_root: str = SANDBOX_SKILLS_PATH,
        state_prefix: str = _STATE_PREFIX,
    ) -> None:
        resolved = root or os.getenv("EDITH_USER_SKILLS_DIR") or _DEFAULT_ROOT
        self._root = Path(resolved)
        # 沙箱侧根目录：用户级 /home/user/skills/custom，项目级 /home/user/skills/project/{ws}
        self._sandbox_root = sandbox_root
        # 共识集合文件名前缀：跨作用域共用会互相删除（见设计稿 D7）
        self._state_prefix = state_prefix
        # 「某用户与某沙箱上次成功同步后，本地副本是什么样」的指纹基线。
        # 必须按 (user, sandbox) 分键：本地内容可能来自另一个沙箱的回拉，
        # 只有跟「这个沙箱」的基线比，才能发现「两边都改过」。
        # 进程内记忆即可——丢了大不了少存一份冲突快照，不会少同步。
        self._baselines: dict[tuple[str, str], tuple] = {}
        # 已经确保过 Skill 目录存在的沙箱（避免每次同步多一次远端 make_dir 往返）
        self._ensured_dirs: set[str] = set()

    # ---- 路径 ----

    @property
    def root(self) -> Path:
        """本地用户 Skill 根目录（所有用户目录的父目录）。"""
        return self._root

    def user_dir(self, user_id: str) -> Path:
        return self._root / _safe_segment(user_id)

    def state_key(self, user_id: str) -> str:
        """删除传播共识集合的键（独立于 workspace 的状态文件）。"""
        return self._state_prefix + _safe_segment(user_id)

    @property
    def sandbox_root(self) -> str:
        """沙箱内运行副本的根目录（索引里的路径行与同步的挂载点都用它）。"""
        return self._sandbox_root

    @property
    def index_base_path(self) -> str:
        """索引「路径」行使用的工作区相对目录（不含 `SKILL.md`）。"""
        return posixpath.relpath(self._sandbox_root, WORKSPACE.root)

    async def ensure(self, user_id: str) -> None:
        # 归属校验放在建目录之前：无权时**一个目录都不创建**，不能先建再删
        # （会留下空壳，让「这个项目有 Skill 目录但读不到内容」变成悬案）
        await self.assert_access(user_id)
        directory = self.user_dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        # 崩溃恢复必须在任何读写之前：上次替换中途退出时，正式目录可能缺项
        if transaction.pending_recovery(directory):
            transaction.recover(directory)
        # 取消与崩溃留下的暂存目录不参与索引/同步，但会一直躺着；按年龄清
        transaction.purge_stale_staging(directory)
        ignore = directory / _IGNORE_FILE
        try:
            current = ignore.read_text(encoding="utf-8") if ignore.is_file() else None
        except OSError:
            current = None
        # 内容不一致就重写（老目录只有 overview.md 一条，需要补上 .conflicts / .staging）
        if current != _IGNORE_CONTENT:
            _atomic_write_text(ignore, _IGNORE_CONTENT)

    async def mount_spec(self, user_id: str) -> MountSpec | None:
        """本地后端没有「挂载」这一步：沙箱目录由 push 填充。"""
        return None

    # ---- 索引 ----

    async def read_index(self, user_id: str) -> SkillIndex:
        """以目录为准重建索引；与磁盘上的 overview.md 不一致时原子回写。

        这就是「索引漂移」这整类故障的终止条件：不再依赖模型记得跑
        `sync_overview.py`，索引是扫出来的，扫完就与目录一致。
        """
        try:
            await self.ensure(user_id)
        except SkillAccessDenied as exc:
            return SkillIndex(status=STATUS_UNAVAILABLE, reason=exc.reason)
        except (OSError, ValueError) as exc:
            return SkillIndex(
                status=STATUS_UNAVAILABLE, reason=f"ensure skill dir failed: {exc}"
            )

        directory = self.user_dir(user_id)
        entries, invalid = self._scan(directory)
        text = render_overview(entries, self.index_base_path)
        healed = False
        target = directory / OVERVIEW_FILENAME
        try:
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            if current != text:
                _atomic_write_text(target, text)
                healed = True
                if current is not None:
                    # 漂移不是错误，但必须留痕：说明有人（模型或旧脚本）写过不一样的索引
                    logger.info(
                        "user skills overview rebuilt user=%s skills=%d invalid=%d",
                        _safe_segment(user_id),
                        len(entries),
                        len(invalid),
                    )
        except OSError as exc:
            # 回写失败不影响本次注入（文本已算好），但索引会继续漂移，必须告警
            logger.warning("write skill overview failed user=%s error=%s", _safe_segment(user_id), exc)

        summaries = [
            SkillSummary(name=e.name, description=e.description, dir_name=e.dir_name)
            for e in entries
        ]
        reason = f"invalid skill dirs: {', '.join(invalid)}" if invalid else ""
        return SkillIndex(
            text=text.strip(),
            summaries=summaries,
            status=STATUS_OK if summaries else STATUS_EMPTY,
            reason=reason,
            healed=healed,
        )

    @staticmethod
    def _scan(directory: Path) -> tuple[list[ScannedSkill], list[str]]:
        """扫描子目录里的 SKILL.md，返回扫描条目与无效目录名。"""
        found: list[ScannedSkill] = []
        invalid: list[str] = []
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return found, invalid
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / "SKILL.md").is_file():
                continue
            try:
                skill = _load_skill(child)
            except Exception:  # noqa: BLE001 - 单个 Skill 格式坏掉不影响其他 Skill
                invalid.append(child.name)
                logger.debug("skip invalid skill dir %s", child.name)
                continue
            found.append(
                ScannedSkill(
                    dir_name=child.name,
                    name=skill.name,
                    description=skill.description,
                )
            )
        return found, invalid

    # ---- 同步 ----

    async def push(self, user_id: str, sandbox) -> SkillSyncResult:
        """权威副本 → 沙箱（含删除传播：本地删掉的 Skill 从沙箱移除）。"""
        try:
            await self.ensure(user_id)
        except SkillAccessDenied as exc:
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=exc.reason)
        except (OSError, ValueError) as exc:
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=str(exc))

        await self._ensure_sandbox_dir(sandbox)
        stats: dict = {}
        try:
            pushed = await _push_impl(
                sandbox,
                self.state_key(user_id),
                self.user_dir(user_id),
                mount=self._sandbox_root,
                stats=stats,
                verify_content=True,
            )
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用，但必须可见
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=str(exc))
        result = _result_from(stats, pushed=pushed)
        if result.status == STATUS_OK:
            # 只有「干净推完」才能声明「这个沙箱见过当前权威内容」。
            # PARTIAL 意味着远端 list 失败或部分文件没写成功 → 没有证据说沙箱与权威一致，
            # 此时推进基线会让随后的一次回拉把沙箱没拿到的那几个 Skill 当作用户删除。
            self._remember_baseline(user_id, sandbox)
        return result

    async def pull(self, user_id: str, sandbox) -> SkillSyncResult:
        """沙箱 → 权威副本，走「暂存 → 校验 → 目录级替换」（D9 / 7.4）。

        覆盖 run_command / 脚本落盘等 write_file 之外的盲区。三条不可让步的性质：

        1. **校验先于更新**：候选内容先落到 `.staging-<ts>/`，校验通过才替换。
           模型把 `SKILL.md` 写坏时，权威副本**零变化**，旧的有效版本继续服务；
        2. **候选版本不消失**：被拒的候选整份移进 `.rejected/<ts>/`，可人工取回；
        3. **不做自动合并**：采用「沙箱覆盖本地」的最后写入者胜出，但覆盖前若本地
           自上次同步后又被改过（可能是另一个会话的编辑），先把旧版本整份快照到
           `.conflicts/<时间戳>/`。最坏情况是多一份可恢复的副本，不是少一份内容。

        另有两处「宁可不更新」的判定，都是同一个原则：**不确定就不要替换**。
        - 拉取过程中出现任何单文件失败 / 熔断 → 候选不完整，丢弃并保留旧版本；
        - 沙箱一个文件都没返回而本地有 Skill → 疑似沙箱被重置（list 拿到空树），
          此时替换会把全部 Skill 删光，直接拒绝。
        """
        try:
            await self.ensure(user_id)
        except SkillAccessDenied as exc:
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=exc.reason)
        except (OSError, ValueError) as exc:
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=str(exc))

        directory = self.user_dir(user_id)
        key = (user_id, self._sandbox_key(sandbox))
        before = self._fingerprint(directory)
        baseline = self._baselines.get(key)
        stash_rel: str | None = None
        if baseline is not None and before is not None and before != baseline:
            # 本地在与「这个沙箱」上次同步之后又被改过（可能是另一个会话的回拉/编辑）：
            # 本次回拉会覆盖这些改动，先整份留存。
            stash_rel = self._stash(directory)

        # 这个沙箱「见过当前权威内容」的唯一证据：本地与它上次成功同步之后
        # 一个字节都没变。只有在这种情况下，候选里缺少某个 Skill 才代表
        # 「用户在沙箱里删了它」；否则那个沙箱可能是 push 之前就过期的
        # （或上次 push 失败，baseline 根本没推进），它的缺失只说明它没见过。
        # 删除是唯一不可逆的操作，宁可少删一次（下一轮 push 会补回来），
        # 也不能让一个过期沙箱把权威副本裁剪掉——真实事故：14 个 Skill 被裁到 13。
        in_sync = baseline is not None and before is not None and before == baseline

        await self._ensure_sandbox_dir(sandbox)
        staging = transaction.new_staging(directory)
        # 暂存目录必须带上同一份忽略清单，否则 overview.md 会被当普通文件拉回来
        (staging / _IGNORE_FILE).write_text(_IGNORE_CONTENT, encoding="utf-8")
        # 共识集合由 `_pull_impl` 在被丢弃的候选上也写过一遍，回滚时要还原
        state_key = self.state_key(user_id)
        saved_paths, saved_sandbox = load_synced_paths(state_key)
        stats: dict = {}
        try:
            pulled = await _pull_impl(
                sandbox,
                state_key,
                staging,
                mount=self._sandbox_root,
                stats=stats,
                verify_content=True,
                # 二进制一并回拉：候选要能完整替代旧目录，否则替换会删掉未回拉的资源
                pull_binaries=True,
                max_file_bytes=SKILL_DIR_MAX_BYTES,
            )
        except asyncio.CancelledError:
            # 从暂存目录建立到 commit 之间只有这一个 await，所以取消只可能落在这里。
            # CancelledError 不是 Exception（3.8+），上面那个分支抓不到它；不清理的话
            # `.staging-*` 会永久留在权威目录里（实测残留过一次）。
            transaction.discard(staging)
            self._restore_state(state_key, saved_paths, saved_sandbox)
            self._drop_stash(directory, stash_rel)
            raise
        except Exception as exc:  # noqa: BLE001 - 同步失败不阻塞工具调用，但必须可见
            transaction.discard(staging)
            self._restore_state(state_key, saved_paths, saved_sandbox)
            self._drop_stash(directory, stash_rel)
            return SkillSyncResult(status=STATUS_UNAVAILABLE, reason=str(exc))

        def abandon(reason: str, status: str = STATUS_PARTIAL) -> SkillSyncResult:
            """丢弃候选、保留旧版本。"""
            transaction.discard(staging)
            self._restore_state(state_key, saved_paths, saved_sandbox)
            self._drop_stash(directory, stash_rel)
            logger.warning(
                "skill pull discarded user=%s scope=%s reason=%s",
                _safe_segment(user_id),
                self._sandbox_root,
                reason,
            )
            return SkillSyncResult(status=status, reason=reason)

        failures = int(stats.get("failures", 0) or 0)
        if stats.get("list_failed"):
            return abandon(
                f"list sandbox skills failed: {stats.get('error', '')}".strip(),
                STATUS_UNAVAILABLE,
            )
        if failures or stats.get("aborted"):
            # 候选不完整：替换等于用残缺内容覆盖完整内容
            return abandon(f"{failures} file(s) failed, candidate discarded")
        skipped = int(stats.get("skipped_large", 0) or 0)
        if skipped:
            # 单文件超限被跳过 → 候选里没有它 → 替换会把它从权威副本删掉。
            # 这是**静默丢文件**的唯一入口，必须拒绝而不是放过。
            return abandon(
                f"candidate incomplete: {skipped} file(s) exceed "
                f"{SKILL_DIR_MAX_BYTES} bytes limit, authoritative copy kept"
            )
        if not pulled and self._has_skills(directory):
            # 沙箱侧一个文件都没拉到而本地有 Skill：疑似沙箱被重置。
            # 这里不替换，等价于旧实现的「整体消失不传播删除」护栏。
            return abandon("sandbox skill tree returned nothing, authoritative copy kept")

        validation = validate_skill_tree(staging, reserved=platform_skill_names())
        if not validation.ok:
            rejected = transaction.reject(staging, directory)
            self._restore_state(state_key, saved_paths, saved_sandbox)
            self._drop_stash(directory, stash_rel)
            reason = f"candidate rejected: {validation.describe()}"
            logger.warning(
                "skill pull rejected user=%s scope=%s reason=%s kept=%s",
                _safe_segment(user_id),
                self._sandbox_root,
                reason,
                rejected or "-",
            )
            return SkillSyncResult(
                status=STATUS_PARTIAL, reason=reason, rejected=[rejected] if rejected else []
            )
        for issue in validation.warnings:
            logger.info("skill pull warning user=%s %s", _safe_segment(user_id), issue.describe())

        pulled_count, deleted_count = _diff_against(staging, directory)
        suppressed = 0
        if not in_sync:
            # 候选不许当删除的证人：只吸收新增与修改，旧目录原样留下。
            suppressed, deleted_count = deleted_count, 0
        try:
            transaction.commit(staging, directory, allow_delete=in_sync)
        except OSError as exc:
            # commit 已保留 `.old-<ts>`，下次 `ensure()` 会把它搬回来
            self._drop_stash(directory, stash_rel)
            return SkillSyncResult(
                status=STATUS_PARTIAL, reason=f"commit failed, will recover: {exc}"
            )

        result = SkillSyncResult(
            pulled=pulled_count,
            deleted=deleted_count,
            status=STATUS_OK,
            reason=(
                f"{suppressed} deletion(s) suppressed: sandbox not in sync with "
                "the authoritative copy"
                if suppressed
                else ""
            ),
        )
        if stash_rel is not None and not result.changed:
            # 实际上什么都没被覆盖：快照是多余的，清掉，别让磁盘上堆垃圾
            self._drop_stash(directory, stash_rel)
            stash_rel = None
        if in_sync:
            # 只有「候选真的见过权威内容」时才推进基线。
            # 被压制的删除不能算同步成功：否则下一轮 pull（run 开始时先拉后推）
            # 会拿着「基线 == 本地」这条已失效的证据放行删除，把同一批 Skill 再删一次。
            # 基线没推进 → 下一轮仍判 stale → 等那一轮的 push 把沙箱补齐后才恢复删除语义。
            self._remember_baseline(user_id, sandbox)
        if stash_rel is None:
            return result
        return replace(result, conflicts=[stash_rel])

    # ---- 沙箱侧 / 本地侧的辅助 ----

    @staticmethod
    def _sandbox_key(sandbox) -> str:
        return getattr(sandbox, "sandbox_id", "") or ""

    async def _ensure_sandbox_dir(self, sandbox) -> None:
        """确保沙箱内 Skill 目录存在（每个沙箱只做一次）。

        Local 后端没有卷挂载，`/home/user/skills/custom` 可能是模板里不存在的目录；
        不先建出来，`files.list` 会失败、`files.write` 也可能因缺父目录失败。
        已存在时报错属正常，忽略；真正的故障留给随后的 list 反映。

        **逐级建父目录**，不只建最深那层：模板只 `make_dir` 了
        `skills/{system,custom}`，项目级的 `skills/project/{ws}` 两级都不存在。
        单次 `make_dir` 是否递归补齐父目录取决于远端实现，押在它身上等于「项目级
        Skill 在现有模板上永远推不进沙箱」——而这条路径的失败形态是 push 返回
        unavailable + 一行 warning，模型只会看到「这个项目没有 Skill」。
        """
        key = self._sandbox_key(sandbox)
        if key and key in self._ensured_dirs:
            return
        for path in sandbox_mkdir_chain(self._sandbox_root):
            with suppress(Exception):  # 已存在 / 无权限：交给随后的 list 反映真实状态
                await sandbox.files.make_dir(path)
        if key:
            self._ensured_dirs.add(key)

    def _fingerprint(self, directory: Path) -> tuple | None:
        try:
            return scan_local_fingerprint(directory)
        except OSError:
            return None

    def _remember_baseline(self, user_id: str, sandbox) -> None:
        fingerprint = self._fingerprint(self.user_dir(user_id))
        if fingerprint is not None:
            self._baselines[(user_id, self._sandbox_key(sandbox))] = fingerprint

    @staticmethod
    def _restore_state(state_key: str, paths: set[str], sandbox_id: str | None) -> None:
        """把共识集合还原到 `_pull_impl` 之前的样子。

        候选被丢弃时，`_pull_impl` 已经按「暂存目录」写过一遍共识集合（那里面
        只有本次拉到的文件）。不还原的话，下一次 pull 的删除传播会基于一份
        残缺的集合判断「沙箱侧消失了什么」，进而误删本地。
        """
        save_synced_paths(state_key, paths, sandbox_id)

    @staticmethod
    def _has_skills(directory: Path) -> bool:
        """权威目录里是否至少有一个真实 Skill（用于判断「空拉取」是否可疑）。"""
        try:
            children = list(directory.iterdir())
        except OSError:
            return False
        return any(
            child.is_dir()
            and not child.name.startswith(".")
            and (child / "SKILL.md").is_file()
            for child in children
        )

    def _has_files(self, directory: Path) -> bool:
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(directory).as_posix()
            if not is_ignored(rel, [OVERVIEW_FILENAME, _IGNORE_FILE, CONFLICT_DIR]):
                return True
        return False

    def _stash(self, directory: Path) -> str | None:
        """把当前本地副本整份快照到 `.conflicts/<时间戳>/`，返回相对路径。"""
        if not self._has_files(directory):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = directory / CONFLICT_DIR / stamp
        copied = 0
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(directory).as_posix()
            if is_ignored(rel, [OVERVIEW_FILENAME, _IGNORE_FILE, CONFLICT_DIR]):
                continue
            try:
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                copied += 1
            except OSError as exc:
                logger.warning("stash skill file %s failed: %s", rel, exc)
        if not copied:
            return None
        relative = f"{CONFLICT_DIR}/{stamp}"
        logger.info(
            "stashed local skills before pull user=%s path=%s files=%d",
            directory.name,
            relative,
            copied,
        )
        return relative

    @staticmethod
    def _drop_stash(directory: Path, relative: str | None) -> None:
        if not relative:
            return
        try:
            shutil.rmtree(directory / relative, ignore_errors=True)
        except OSError:
            pass

    def local_fingerprint(self, user_id: str) -> tuple | None:
        try:
            return scan_local_fingerprint(self.user_dir(user_id))
        except (OSError, ValueError):
            return None


def _diff_against(staging: Path, directory: Path) -> tuple[int, int]:
    """候选目录 vs 现有权威目录：返回 (新增或内容不同, 权威副本有而候选没有) 的文件数。

    刻意按内容比而不是按大小比：`verify_content=True` 的存在理由就是「等长改写
    也要被发现」，计数口径必须与同步口径一致，否则会出现「同步了但报告 0 处变更」。
    只跑本地 IO（Skill 全是 KB 级），可接受。
    """
    def _files(root: Path) -> dict[str, Path]:
        found: dict[str, Path] = {}
        try:
            children = list(root.rglob("*"))
        except OSError:
            return found
        for path in children:
            try:
                if path.is_file():
                    found[path.relative_to(root).as_posix()] = path
            except OSError:
                continue
        return found

    candidate = _files(staging)
    current = _files(directory)
    patterns = [line for line in _IGNORE_CONTENT.splitlines() if line]
    changed = 0
    for rel, path in candidate.items():
        if is_ignored(rel, patterns):
            continue
        other = current.get(rel)
        try:
            if other is None or other.read_bytes() != path.read_bytes():
                changed += 1
        except OSError:
            changed += 1
    # 服务端文件（overview.md / .edithignore）不在候选里，但它们的"消失"不是删除
    deleted = sum(
        1
        for rel in current
        if rel not in candidate and not is_ignored(rel, patterns)
    )
    return changed, deleted


def _result_from(
    stats: dict, *, pushed: int = 0, pulled: int = 0
) -> SkillSyncResult:
    """把 sync.py 的观测统计翻译成同步结果（失败原因必须带回来）。"""
    deleted = int(stats.get("deleted", 0) or 0)
    failures = int(stats.get("failures", 0) or 0)
    if stats.get("list_failed"):
        return SkillSyncResult(
            pushed=pushed,
            pulled=pulled,
            deleted=deleted,
            status=STATUS_UNAVAILABLE,
            reason=f"list sandbox skills failed: {stats.get('error', '')}".strip(),
        )
    if failures:
        return SkillSyncResult(
            pushed=pushed,
            pulled=pulled,
            deleted=deleted,
            status=STATUS_PARTIAL,
            reason=f"{failures} file(s) failed{', aborted' if stats.get('aborted') else ''}",
        )
    if stats.get("push_uncompared"):
        return SkillSyncResult(
            pushed=pushed,
            pulled=pulled,
            deleted=deleted,
            status=STATUS_PARTIAL,
            reason="sandbox list failed, pushed without compare",
        )
    return SkillSyncResult(pushed=pushed, pulled=pulled, deleted=deleted, status=STATUS_OK)
