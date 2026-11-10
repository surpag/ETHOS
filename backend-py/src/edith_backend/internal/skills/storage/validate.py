"""同步前的四类校验：格式 / 保留名 / 数量 / 体积（设计稿 7.6 / D9）。

为什么校验必须在「暂存目录」上做
------------------------------
校验的对象是「这一次的候选内容」，不是「磁盘上已经能用的东西」。校验失败时
**磁盘上的旧版本必须继续服务** —— 所以校验点只能在 staging 上，且失败时整份
移进 `.rejected/<ts>/`，绝不删除任何已存在的内容（见 `transaction.py`）。

两种严重性，刻意分开
------------------
- `errors`：会让索引/路径失真的问题（frontmatter 坏、名字非法、目录名与 name
  不一致、超量超体积）→ **整份拒绝**。
- `warnings`：不构成 Skill 的目录（没有 SKILL.md）、不影响索引的杂项 →
  **放行但上报**。把它们也判死会让「沙箱里多了一个 notes/ 目录」阻断用户
  所有的 Skill 编辑，那是另一种形式的不可用。

目录名必须等于 frontmatter 的 `name` 是硬错误，不是洁癖：B2 起 `entries` 的
`sandbox_path` 由**目录名**拼出，两者不一致时索引里的路径与真实目录会分叉
（旧实现正是这个 bug：模型按索引读文件，读到的是「空 Skill」而不是报错）。
限值只作用于存储侧，不是注入侧配额（那是 D10 的 L1，见设计稿 10.1）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..catalog import _load_skill

#: 单个 SKILL.md 上限：L2 单 run 共 60KB，单份超过 32KB 基本等于「把数据塞进正文」
SKILL_MD_MAX_BYTES = 32 * 1024
#: 单个 Skill 目录上限（含 references/ scripts/ assets/）
SKILL_DIR_MAX_BYTES = 2 * 1024 * 1024
#: 单作用域 Skill 数上限：L1 4000 字符约容 40 条摘要，100 是存储上限而非注入上限
SKILL_COUNT_MAX = 100

#: 与 `skill-creator/scripts/quick_validate.py` 同一规则（小写字母/数字/单连字符）
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

KIND_FORMAT = "format"
KIND_RESERVED = "reserved"
KIND_COUNT = "count"
KIND_SIZE = "size"


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题；`skill` 是目录名（不是 frontmatter name，坏掉的没有 name）。"""

    skill: str
    kind: str
    reason: str

    def describe(self) -> str:
        return f"{self.skill}: {self.reason}"


@dataclass(frozen=True)
class ValidationResult:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    #: 通过格式校验的 Skill 名（顺序与目录名一致），供调用方做集合断言
    names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def describe(self) -> str:
        if self.errors:
            shown = "; ".join(issue.describe() for issue in self.errors[:3])
            extra = len(self.errors) - 3
            return shown + (f"; +{extra} more" if extra > 0 else "")
        return "; ".join(issue.describe() for issue in self.warnings[:3])


def platform_skill_names() -> frozenset[str]:
    """平台 Skill 名单（D2 / D8 保留名）。

    每次现扫而不缓存：平台 Skill 随沙箱模板发布，进程内缓存会让「模板里新装了
    一个同名 Skill」这件事滞后一整个进程生命周期，而保留名冲突的后果是
    「用户的自定义 Skill 被静默淘汰」。
    """
    from ..catalog import load_catalog

    return frozenset(skill.name for skill in load_catalog())


def _dir_bytes(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def validate_skill_tree(
    root: Path,
    *,
    reserved: frozenset[str] | set[str] | None = None,
    max_count: int = SKILL_COUNT_MAX,
    max_skill_md_bytes: int = SKILL_MD_MAX_BYTES,
    max_dir_bytes: int = SKILL_DIR_MAX_BYTES,
) -> ValidationResult:
    """校验一棵 Skill 目录树；`root` 是 Skill 的父目录（staging 根）。"""
    result = ValidationResult()
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        result.errors.append(
            ValidationIssue("<root>", KIND_FORMAT, f"cannot scan directory: {exc}")
        )
        return result

    reserved = reserved or frozenset()
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            # 不是 Skill：索引本来就会跳过它。判死会让一个杂项目录阻断全部编辑。
            result.warnings.append(
                ValidationIssue(child.name, KIND_FORMAT, "missing SKILL.md (ignored)")
            )
            continue
        try:
            skill = _load_skill(child)
        except Exception as exc:  # noqa: BLE001 - 解析失败本身就是校验结论
            result.errors.append(
                ValidationIssue(child.name, KIND_FORMAT, f"invalid SKILL.md: {exc}")
            )
            continue

        if not NAME_PATTERN.match(skill.name):
            result.errors.append(
                ValidationIssue(
                    child.name,
                    KIND_FORMAT,
                    f"name {skill.name!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$",
                )
            )
            continue
        if skill.name != child.name:
            result.errors.append(
                ValidationIssue(
                    child.name,
                    KIND_FORMAT,
                    f"directory name must equal frontmatter name {skill.name!r}",
                )
            )
            continue
        if skill.name in reserved:
            result.errors.append(
                ValidationIssue(
                    child.name,
                    KIND_RESERVED,
                    f"name {skill.name!r} is reserved by a platform skill",
                )
            )
            continue

        try:
            size = skill_md.stat().st_size
        except OSError:
            size = 0
        if size > max_skill_md_bytes:
            result.errors.append(
                ValidationIssue(
                    child.name,
                    KIND_SIZE,
                    f"SKILL.md {size} bytes exceeds {max_skill_md_bytes}",
                )
            )
            continue
        total = _dir_bytes(child)
        if total > max_dir_bytes:
            result.errors.append(
                ValidationIssue(
                    child.name,
                    KIND_SIZE,
                    f"skill dir {total} bytes exceeds {max_dir_bytes}",
                )
            )
            continue

        result.names.append(skill.name)

    if len(result.names) > max_count:
        result.errors.append(
            ValidationIssue(
                "<root>",
                KIND_COUNT,
                f"{len(result.names)} skills exceed limit {max_count}",
            )
        )
    return result
