"""volume 的类型定义。

对应 Go 版 internal/volume/types.go。
"""

from __future__ import annotations

from dataclasses import dataclass

# 用户 Volume 在 Sandbox 中的固定挂载路径
CUSTOM_SKILLS_PATH = "/home/user/skills/custom"
# 用户 Volume 中 Skills 摘要索引的路径
USER_OVERVIEW_PATH = "/overview.md"


@dataclass
class UserVolume:
    """后端内部使用的用户 Volume 基本信息。"""

    id: str
    name: str


@dataclass
class Mount:
    """Sandbox 创建时需要的 Volume 挂载信息。"""

    name: str
    path: str
