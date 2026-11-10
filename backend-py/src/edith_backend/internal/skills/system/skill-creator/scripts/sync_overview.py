#!/usr/bin/env python3
"""为指定 Skill 目录生成一份**沙箱内临时**预览 overview.md。

不是权威索引：服务端扫描目录自行派生索引，这个文件不会同步回去。
"""

import argparse
from pathlib import Path

from quick_validate import (
    FRONTMATTER_KEYS,
    parse_simple_yaml,
    split_frontmatter,
    validate_skill,
)

#: Sandbox 根目录，用于把绝对路径还原成文件工具认识的工作区相对路径。
SANDBOX_ROOT = "/home/user"


def display_prefix(root):
    """把 Skill 根目录映射成索引里展示的路径前缀（如 `skills/custom`）。

    不能写死 `skills/custom`：指到项目级目录时会输出一个不存在的路径，
    模型照着读只会得到「空的 Skill」而不是报错。
    """
    posix = root.as_posix().rstrip("/")
    if posix.startswith(SANDBOX_ROOT + "/"):
        return posix[len(SANDBOX_ROOT) + 1 :]
    return posix


def skill_summary(skill_path):
    """读取并校验一个 Skill，返回 (目录名, name, description)。

    目录名单独返回：索引的显示名用 frontmatter 的 `name`，但路径必须用真实目录名。
    两者不一致时（人工改过 frontmatter、或从别处拷来的 Skill），用 name 拼路径会写出
    一个不存在的目录，模型据此读文件只会得到「空的 Skill」而不是报错。
    """
    valid, message = validate_skill(skill_path)
    if not valid:
        raise ValueError(f"{skill_path.name}: {message}")

    content = (skill_path / "SKILL.md").read_text(encoding="utf-8-sig")
    frontmatter_lines, _, error = split_frontmatter(content)
    if error:
        raise ValueError(f"{skill_path.name}: {error}")
    frontmatter, error = parse_simple_yaml(frontmatter_lines, FRONTMATTER_KEYS)
    if error:
        raise ValueError(f"{skill_path.name}: {error}")
    return skill_path.name, frontmatter["name"], frontmatter["description"]


def build_overview(root):
    """扫描 root 目录并生成 overview.md 内容。"""
    prefix = display_prefix(root)
    skills = []
    for skill_path in sorted(root.iterdir(), key=lambda path: path.name):
        if not skill_path.is_dir() or skill_path.name.startswith("."):
            continue
        skills.append(skill_summary(skill_path))

    lines = [
        f"# Skills 总览：{prefix}",
        "",
        "以下内容由脚本生成，仅为本目录的临时预览，请勿手动修改。",
        "服务端索引由扫描 Skill 目录派生，与本文件无关。",
        "",
    ]
    if not skills:
        lines.append("当前目录下没有 Skill。")
    else:
        for dir_name, name, description in sorted(skills, key=lambda item: item[1]):
            lines.append(f"- `{name}`：{description}")
            lines.append(f"  - 路径：`{prefix}/{dir_name}/SKILL.md`")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="生成指定 Skill 目录的临时总览")
    parser.add_argument(
        "--path",
        default="/home/user/skills/custom",
        help="Skill 根目录，默认用户级 /home/user/skills/custom",
    )
    args = parser.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"错误: Skills 目录不存在: {root}")
        return 1

    try:
        overview = build_overview(root)
        (root / "overview.md").write_text(overview, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"错误: {error}")
        return 1

    print(f"已生成临时总览: {root / 'overview.md'}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
