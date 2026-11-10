"""用户 Skill 同步测试用的假沙箱。

只实现 Skill 同步真正用到的那几个方法（`files.list / read / write / remove /
make_dir`），语义对齐 E2B：目录不存在时 `list` 抛错、`remove` 对目录递归。
"""

from __future__ import annotations

from dataclasses import dataclass

from e2b import FileType

MOUNT = "/home/user/skills/custom"
#: 模板构建时就存在的目录（`/home/user` 由 `e2bdev/base` 提供）
_PREEXISTING = {"/", "/home", "/home/user"}


@dataclass
class FakeEntry:
    path: str
    size: int
    type: FileType = FileType.FILE


class FakeSandboxFiles:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.dirs: set[str] = {MOUNT}
        # 故障注入：模拟远端不可用 / 单文件读失败
        self.list_error: Exception | None = None
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.remove_calls: list[str] = []
        #: 严格建目录：父目录不存在就报错。
        #: 默认关闭（多数测试不关心目录层次）；打开后用于钉住「不能押远端递归建父目录」，
        #: 因为默认实现会替调用方补齐所有祖先，恰好把这类 bug 掩盖掉。
        self.strict_mkdir = False

    # ---- 便捷写入（模拟 Agent 在沙箱里落盘，不经过工具层）----

    def write_now(self, path: str, data: bytes) -> None:
        self.blobs[path] = data
        parts = path.strip("/").split("/")[:-1]
        for index in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:index]))

    def tree(self) -> list[str]:
        prefix = MOUNT + "/"
        return sorted(p[len(prefix):] for p in self.blobs if p.startswith(prefix))

    # ---- E2B 文件系统接口 ----

    async def make_dir(self, path: str, **_kwargs) -> None:
        parts = path.strip("/").split("/")
        if self.strict_mkdir:
            parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
            if parent not in self.dirs and parent not in _PREEXISTING:
                raise FileNotFoundError(f"parent dir not found: {parent}")
            self.dirs.add("/" + "/".join(parts))
            return
        for index in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:index]))

    async def list(self, path: str, depth: int = 1, **_kwargs) -> list[FakeEntry]:
        if self.list_error is not None:
            raise self.list_error
        base = path.rstrip("/")
        if base not in self.dirs and not any(
            key.startswith(base + "/") for key in self.blobs
        ):
            raise FileNotFoundError(base)
        prefix = base + "/"
        entries = [
            FakeEntry(path=directory, size=0, type=FileType.DIR)
            for directory in sorted(self.dirs)
            if directory.startswith(prefix)
        ]
        entries.extend(
            FakeEntry(path=key, size=len(value), type=FileType.FILE)
            for key, value in sorted(self.blobs.items())
            if key.startswith(prefix)
        )
        return entries

    async def read(
        self, path: str, format: str = "text", request_timeout: float | None = None
    ):
        self.read_calls.append(path)
        if self.read_error is not None:
            raise self.read_error
        if path not in self.blobs:
            raise FileNotFoundError(path)
        return bytearray(self.blobs[path])

    async def write(self, path: str, data, **_kwargs) -> None:
        self.write_calls.append(path)
        if self.write_error is not None:
            raise self.write_error
        self.write_now(path, bytes(data))

    async def remove(self, path: str, **_kwargs) -> None:
        self.remove_calls.append(path)
        if path in self.blobs:
            del self.blobs[path]
            return
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self.blobs if k.startswith(prefix)]:
            del self.blobs[key]
        for directory in [d for d in self.dirs if d == path or d.startswith(prefix)]:
            self.dirs.discard(directory)


class FakeSkillSandbox:
    def __init__(self, sandbox_id: str = "sbx-1") -> None:
        self.files = FakeSandboxFiles()
        self.sandbox_id = sandbox_id

    # ---- 断言辅助 ----

    def put(self, rel: str, text: str) -> None:
        """模拟 Agent 直接往沙箱写文件（run_command / 脚本落盘）。"""
        self.files.write_now(f"{MOUNT}/{rel}", text.encode("utf-8"))

    def get(self, rel: str) -> str:
        return self.files.blobs[f"{MOUNT}/{rel}"].decode("utf-8")

    def has(self, rel: str) -> bool:
        return f"{MOUNT}/{rel}" in self.files.blobs

    def skill_files(self) -> list[str]:
        """沙箱里属于 Skill 的文件（排除服务端派生的 overview.md）。"""
        return [rel for rel in self.files.tree() if rel != "overview.md"]


def skill_md(name: str, description: str) -> str:
    """生成一份合法的 SKILL.md（frontmatter 必填 name + description）。"""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "按此说明执行。\n"
    )
