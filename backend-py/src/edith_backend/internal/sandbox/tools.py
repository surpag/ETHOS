"""sandbox 的 Agent 工具集：文件与进程操作。

对应 Go 版 internal/sandbox/tools.go + files.go + processes.go。
方案 B 用 OpenAI function calling 格式定义工具，execute 方法统一分发。
"""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
from collections.abc import Awaitable, Callable

from e2b import AsyncSandbox, FileType
from e2b.sandbox.commands.command_handle import CommandExitException

from ..gitops import (
    CloneUrlRejected,
    GitCommandRejected,
    resolve_git_identity,
    run_git,
)
from ..tools import COMMAND_ERROR, TOOL_FAILURE, command_stderr_hint, tool_error_result
from ..workspace.volume_backend import WorkspaceSyncError
from .service import Service
from .workspace import ALLOWED_BASE_DIRS, WORKSPACE, relative_path, workspace_path

logger = logging.getLogger(__name__)

DEFAULT_READ_LINES = 200  # read_file 默认读取行数（按行，替换原 32KB 字节截断）
DEFAULT_COMMAND_TIMEOUT_MS = 60_000
MAX_COMMAND_OUTPUT = 32 * 1024


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _guide(action: str) -> str:
    return action + "\n\n" + WORKSPACE.tool_guide()


def _join_argv(command: str, argv: list[str] | None) -> str:
    """把程序名与参数数组合并成完整命令行。

    e2b 2.x 的 `Commands.run(cmd)` 只接收完整命令字符串（旧版 API 才有 `args` 参数），
    因此当模型把参数拆进 `args` 数组时，需在此合并；`argv` 为空时原样返回 command
    （此时模型通常已把完整命令写在 command 里）。
    """
    if not argv:
        return command
    return " ".join([command, *(shlex.quote(str(a)) for a in argv)])


# 需要做工作区路径补全的文件类工具（进程类工具不动，cwd 固定 work/）
# 本次新增：配合 workspace_id 的「隐式工作区」能力（Web 场景模型无当前目录概念）
_FILE_TOOLS = {
    "sandbox_list_files", "sandbox_read_file", "sandbox_write_file",
    "sandbox_make_directory", "sandbox_move_path", "sandbox_delete_path",
}

# 方案 B（B2/B3）：可能改变沙箱文件内容的工具 → 触发本地↔沙箱增量同步
_MUTATING_TOOLS = {
    "sandbox_write_file", "sandbox_run_command",
    "sandbox_make_directory", "sandbox_move_path", "sandbox_delete_path",
}

# 各文件类工具需要做「工作区授权校验」的路径字段。
# 新增文件类工具时必须同步登记（tests/test_stage10.py 有断言，漏登记会测试失败而不是静默放行）。
_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "sandbox_list_files": ("path",),
    "sandbox_read_file": ("path",),
    "sandbox_write_file": ("path",),
    "sandbox_make_directory": ("path",),
    "sandbox_delete_path": ("path",),
    "sandbox_move_path": ("from", "to"),
}

# 审计摘要要跳过的"大字段"（文件内容），避免把正文写进日志
_BULKY_ARG_KEYS = {"content", "data"}


def _complete_workspace_path(path: str, workspace_id: str) -> str:
    """把模型传入的路径补全为 workspace/{workspace_id}/... 下。

    - 空 / '.' → 工作区根
    - 显式顶层目录（uploads/ work/ artifacts/ skills/ workspace/）→ 原样
    - 其余 → 视为工作区内相对路径，自动补前缀
    """
    if not workspace_id:
        return path
    relative = (path or "").strip().replace("\\", "/")
    if not relative or relative == ".":
        return f"workspace/{workspace_id}"
    if relative.split("/")[0] in ALLOWED_BASE_DIRS:
        return relative
    return f"workspace/{workspace_id}/{relative}"


def _scope_violation(name: str, args: dict, workspace_id: str) -> str:
    """检查补全后的路径是否越出当前工作区。

    返回越权原因（可读、供模型自救），未越权返回空串。
    这是唯一的授权卡点：所有文件类工具、所有路径字段都经此校验，
    新增工具只要登记到 _PATH_FIELDS 就自动受约束。
    """
    for key in _PATH_FIELDS.get(name, ()):
        value = args.get(key)
        if value is None:
            continue
        try:
            workspace_path(value, allow_root=True, current_workspace_id=workspace_id)
        except PermissionError as exc:
            return str(exc)
        except ValueError:
            # 绝对路径 / '..' 等格式问题由各 _op_* 内部校验并转成工具错误，此处不重复报
            continue
    return ""


# 敏感路径段：Agent 不应读写/删除。.git 是 git 能力根基（删了 git 工具就废），
# .env / SSH 私钥 / 凭据文件是真实密钥——任何文件类工具都不该碰。
_SENSITIVE_SEGMENTS = {
    ".git", ".hg", ".svn",                                    # 版本库元数据
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",             # SSH 私钥（.pub 公钥放行）
    "credentials", "credentials.json", "auth.json",           # 凭据文件
    ".netrc", "netrc",
}


def _sensitive_path_violation(name: str, args: dict) -> str:
    """检查文件工具的目标路径是否命中敏感段（.git / .env / 私钥 / 凭据）。

    返回命中的敏感段说明（供模型自救），未命中返回空串。
    """
    for key in _PATH_FIELDS.get(name, ()):
        value = args.get(key)
        if not value:
            continue
        for part in str(value).replace("\\", "/").split("/"):
            if not part:
                continue
            low = part.lower()
            if low in _SENSITIVE_SEGMENTS or low == ".env" or low.startswith(".env."):
                return part
    return ""


def _audit_digest(args: dict, limit: int = 300) -> str:
    """审计摘要：跳过文件内容等大字段，只保留结构与关键参数（避免日志被正文撑爆）。"""
    if not isinstance(args, dict):
        return repr(args)[:limit]
    parts = []
    for key, value in args.items():
        if key in _BULKY_ARG_KEYS:
            parts.append(f"{key}=<{len(str(value))} chars>")
        else:
            parts.append(f"{key}={value}")
    text = " ".join(parts)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(len={len(text)})"


async def _list_dir_names(sandbox: AsyncSandbox, directory: str) -> list[str]:
    """列出目录下的条目名（仅用于错误提示）；目录不存在或失败时静默返回空列表。"""
    try:
        entries = await sandbox.files.list(directory)
        return [e.name for e in entries]
    except Exception:  # noqa: BLE001 - 仅用于增强错误提示，失败不阻塞
        return []


def _workspace_root_of(path: str) -> str:
    """从工作区绝对路径推断 workspace/{id} 边界（空目录回收不得越界）。"""
    parts = path.split("/")
    try:
        idx = parts.index(WORKSPACE.workspace)
    except ValueError:
        return ""
    if len(parts) < idx + 2:
        return ""
    return "/".join(parts[: idx + 2])


async def _prune_empty_parents(sandbox: AsyncSandbox, file_path: str) -> list[str]:
    """删除文件/目录后回收空父目录，上溯到 workspace/{id} 根为止。

    为什么需要：`sandbox_delete_path mp/agent-learn` 只会删掉子目录，父目录 `mp`
    留成空壳，文件列表里这个名字还在 —— 用户与 Agent 都容易误判为「没删掉」。

    安全性：E2B `files.remove` 对目录是**递归删除**的，所以必须先 list 确认为空
    才能删；否则会把父目录下其它未参与本次删除的文件一起删掉。
    """
    root = _workspace_root_of(file_path)
    if not root:
        return []
    reclaimed: list[str] = []
    current = posixpath.dirname(file_path)
    while current.startswith(root + "/"):
        try:
            remaining = await sandbox.files.list(current, depth=1)
        except Exception:  # noqa: BLE001 - 目录不存在：停止上溯
            break
        if remaining:
            break
        try:
            await sandbox.files.remove(current)
        except Exception:  # noqa: BLE001 - 并发写入 / 已不存在
            break
        reclaimed.append(relative_path(current))
        current = posixpath.dirname(current)
    return reclaimed


_LARGE_FILE_BYTES = 512 * 1024  # 超过此大小改用 sed 按行读，避免全量传输


async def _read_file_slice(
    sandbox: AsyncSandbox, file_path: str, start_line: int, limit: int
) -> tuple[list[str], int, int]:
    """读取文件指定行区间，返回 (选中行, 总行数, 结束行)。

    小文件直读后切片（简单可靠）；大文件用 sed 按行读 + wc -l 数行，避免全量传输。
    """
    try:
        size = (await sandbox.files.get_info(file_path)).size or 0
    except Exception:  # noqa: BLE001 - 取不到大小则回退直读（read 会再抛 FileNotFoundError）
        size = 0

    if size <= _LARGE_FILE_BYTES:
        data = await sandbox.files.read(file_path)
        all_lines = data.splitlines()
        total = len(all_lines)
        if start_line > total:
            return [], total, total
        end = min(start_line + limit - 1, total)
        return all_lines[start_line - 1 : end], total, end

    quoted = shlex.quote(file_path)
    wc = await sandbox.commands.run(f"wc -l < {quoted}")
    total = int((wc.stdout or "0").strip() or "0")
    if start_line > total:
        return [], total, total
    end = min(start_line + limit - 1, total)
    sed = await sandbox.commands.run(f"sed -n '{start_line},{end}p' {quoted}")
    return (sed.stdout or "").splitlines(), total, end


class SandboxToolSet:
    """Sandbox 自己提供的文件与进程工具集合。"""

    def __init__(
        self,
        service: Service,
        git_identity_provider: Callable[[str], Awaitable[tuple[str, str]]] | None = None,
        push_workspace: Callable[[str, str, str, str, str], Awaitable[dict]] | None = None,
        remote_info: Callable[[str, str], Awaitable[dict]] | None = None,
    ) -> None:
        self._service = service
        # 解析用户的 git 提交身份（user_id → (name, email)）；未注入或失败时用平台默认
        self._git_identity_provider = git_identity_provider
        # 推送能力由组合根注入（签名：user_id, session_id, workspace_id, branch,
        # remote_branch -> 结果）。
        # 用可调用对象注入而不是 import gitops.orchestrator：后者依赖 sandbox.service，
        # 直接 import 会形成 sandbox ↔ gitops 循环。推送实现只有一份（gitops.push/orchestrator），
        # 本工具是它的 Agent 入口。
        self._push_workspace = push_workspace
        # 只读：远端分支列表 + 默认分支。让模型推送前能看清远端真相，
        # 而不是凭「本地分支叫什么」猜目标分支（真实事故）。
        self._remote_info = remote_info

    async def _resolve_git_identity(self, user_id: str) -> tuple[str, str]:
        """解析提交身份：用户设置优先，失败回退平台默认（不阻塞 git 调用）。"""
        if self._git_identity_provider is None or not user_id:
            return resolve_git_identity()
        try:
            return await self._git_identity_provider(user_id)
        except Exception as exc:  # noqa: BLE001 - 身份解析失败不阻塞 git
            logger.warning("resolve git identity failed user=%s: %s", user_id, exc)
            return resolve_git_identity()

    def tool_definitions(self) -> list[dict]:
        return [
            _tool("sandbox_list_files", _guide("列出当前会话 Sandbox 工作区中的文件和目录。"),
                  {"path": {"type": "string", "description": "Workspace-relative directory"},
                   "depth": {"type": "integer", "description": "Directory depth; default 1"}}, []),
            _tool("sandbox_read_file", _guide("读取当前会话 Sandbox 工作区中的文本文件。"),
                  {"path": {"type": "string", "description": "Workspace-relative text file"},
                   "offset": {"type": "integer", "description": "起始行号（1-based，默认 1）"},
                   "limit": {"type": "integer", "description": "读取行数（默认 200 行）"}}, ["path"]),
            _tool("sandbox_write_file", _guide("在当前会话 Sandbox 中创建或覆盖文本文件。"),
                  {"path": {"type": "string", "description": "Workspace-relative file"},
                   "content": {"type": "string", "description": "Text content"}}, ["path", "content"]),
            _tool("sandbox_make_directory", _guide("创建目录，包括缺失的父目录。"),
                  {"path": {"type": "string", "description": "Workspace-relative path"}}, ["path"]),
            _tool("sandbox_move_path", _guide("移动或重命名当前会话 Sandbox 中的文件或目录。"),
                  {"from": {"type": "string", "description": "Source path"},
                   "to": {"type": "string", "description": "Destination path"}}, ["from", "to"]),
            _tool("sandbox_delete_path", _guide("删除当前会话 Sandbox 中的文件或目录。"),
                  {"path": {"type": "string", "description": "Workspace-relative path"}}, ["path"]),
            _tool("sandbox_run_command", _guide(
                      "在当前会话 Sandbox 的 work/ 目录中前台执行程序，并返回输出。"
                      "注意：命令不受工作区路径白名单限制，可在沙箱内访问任意路径；"
                      "只有落在当前工作区内的改动才会同步回用户本地目录。"),
                  {"command": {"type": "string", "description": "Program to execute"},
                   "args": {"type": "array", "items": {"type": "string"}},
                   "envs": {"type": "object", "additionalProperties": {"type": "string"}},
                   "timeoutMs": {"type": "integer"}}, ["command"]),
            _tool("sandbox_start_process", _guide("在当前会话 Sandbox 的 work/ 目录中启动长期进程。"),
                  {"command": {"type": "string", "description": "Long-running program"},
                   "args": {"type": "array", "items": {"type": "string"}}}, ["command"]),
            _tool("sandbox_list_processes", _guide("列出当前会话 Sandbox 中正在运行的进程。"), {}, []),
            _tool("sandbox_kill_process", _guide("结束当前会话 Sandbox 中指定 PID 的进程。"),
                  {"pid": {"type": "integer", "description": "Sandbox process ID"}}, ["pid"]),
            _tool("git", _guide(
                      "在当前会话 Sandbox 的工作区根目录执行 git 命令。支持：只读（status/log/diff/"
                      "show/blame 等）、clone 公网仓库、本地写（add 显式路径 / commit -m 消息）。"
                      "**推送到远端请用 git_push**（沙箱里没有凭据，push/pull 一律被白名单拒绝）。"
                      "clone 到根目录（clone <url> .）要求工作区为空——"
                      "若已有文件会报错，请先与用户确认是否清理，或先克隆到临时目录再用 run_command 移入。"
                      "命令经白名单校验，clone 的 URL 须为公网 https/ssh，内网地址会被拒绝；"
                      "注意 git 操作请用本工具，不要用 run_command。"),
                  {"subcommand": {"type": "string", "description": "git 子命令，如 status / log / diff / clone / add / commit"},
                   "args": {"type": "array", "items": {"type": "string"},
                            "description": "额外参数，如 ['--oneline', '-10']；commit 需含 ['-m', '消息']"}}, ["subcommand"]),
            _tool("git_push", _guide(
                      "把当前工作区**已提交**的改动推送到该工作区绑定的远端仓库（GitHub）。"
                      "推送由后端带着用户已保存的凭据完成，沙箱内不接触凭据，所以这一条不需要也不允许"
                      "用 git 工具执行 push。先 git add + git commit，再调用本工具；"
                      "分支可以换名：branch 是沙箱里的本地分支（留空=当前分支），remoteBranch 是远端"
                      "要更新的分支（留空=与 branch 同名），例如本地 master 推到远端 main 或推到新分支"
                      " docs/readme。禁止强推（--force/--mirror/删远端分支都被拒）。"
                      "若返回 push_non_fast_forward：说明远端该分支已有本地没有的提交（或历史不相关），"
                      "**不要重复重试**，改用一个新的 remoteBranch 推上去再让用户开 PR，或先与用户确认"
                      "是否要基于远端分支重建提交。不确定远端有什么分支时，先调 git_remote_branches。"),
                  {"branch": {"type": "string",
                              "description": "沙箱内的本地分支；留空使用当前分支"},
                   "remoteBranch": {"type": "string",
                                    "description": "远端目标分支；留空则与 branch 同名"}}, []),
            _tool("git_remote_branches", _guide(
                      "只读：查看该工作区绑定的远端仓库有哪些分支、默认分支是哪个（带用户凭据查询）。"
                      "推送前先用它确认远端现状，避免把提交撞到一个历史完全无关的分支上。"
                      "返回 remoteUrl / defaultBranch / branches[{name, sha}]。"),
                  {}, []),
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",
    ) -> str:
        """统一执行入口。

        本次修改：新增 workspace_id 参数（默认空串，向后兼容）——
        Web 场景下模型没有「当前目录」概念，由工具层在文件类工具执行前
        自动把相对路径补全到 workspace/{workspace_id}/ 下，模型无需拼 ID。
        """
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown sandbox tool: {name}", "kind": TOOL_FAILURE})
        if name == "git_remote_branches":
            # 只读远端查询不依赖沙箱：不该为一次查询去拉 E2B（秒级 + 有成本）
            try:
                return json.dumps(
                    await self._op_git_remote_branches(user_id, workspace_id),
                    ensure_ascii=False,
                )
            except Exception as exc:  # noqa: BLE001 - 与下方统一错误出口一致
                return tool_error_result(name, exc)
        try:
            sandbox = await self._service.workspace(user_id, session_id, workspace_id)
        except WorkspaceSyncError as exc:
            # 工作区文件同步未完成：暂时性、可重试，明确提示 Agent 稍后重试，而非拿到空沙箱盲目执行
            return json.dumps(
                {
                    "error": str(exc),
                    "kind": COMMAND_ERROR,
                    "retryable": True,
                    "suggestion": "工作区文件尚未同步到沙箱，请稍后重试；若持续失败，请告知用户检查工作区上传是否完整。",
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - E2B 不可用降级：工具结果返回错误，Agent 转纯文本
            logger.warning("sandbox unavailable tool=%s user=%s error=%s", name, user_id, exc)
            return json.dumps(
                {"error": f"sandbox unavailable: {exc}", "kind": TOOL_FAILURE, "retryable": False},
                ensure_ascii=False,
            )
        try:
            if name in _FILE_TOOLS and workspace_id:
                arguments = self._complete_arguments(name, arguments, workspace_id)
                # D1 授权校验：一个 session 只能访问自己的工作区
                violation = _scope_violation(name, arguments, workspace_id)
                if violation:
                    logger.warning(
                        "cross-workspace access blocked tool=%s user=%s session=%s workspace=%s: %s",
                        name,
                        user_id,
                        session_id,
                        workspace_id,
                        violation,
                    )
                    return json.dumps(
                        {
                            "error": f"路径越权：{violation}",
                            "kind": COMMAND_ERROR,
                            "retryable": True,
                            "suggestion": (
                                "只能访问当前工作区内的文件。请改用工作区内相对路径"
                                "（如 docs/report.md），会自动补全到当前工作区。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                # P2-6 敏感路径：.git/.env/私钥/凭据文件一律拒绝（与授权校验正交）
                sensitive = _sensitive_path_violation(name, arguments)
                if sensitive:
                    logger.warning(
                        "sensitive path blocked tool=%s user=%s session=%s workspace=%s: %r",
                        name,
                        user_id,
                        session_id,
                        workspace_id,
                        sensitive,
                    )
                    return json.dumps(
                        {
                            "error": f"敏感路径不可访问：{sensitive!r}",
                            "kind": COMMAND_ERROR,
                            "retryable": True,
                            "suggestion": (
                                f"路径含敏感段 {sensitive!r}（版本库元数据 / 密钥文件），"
                                "Agent 不允许读写或删除。请改操作其他文件。"
                            ),
                        },
                        ensure_ascii=False,
                    )
            # B3：写类工具执行前，先把本地新增/修改推送到沙箱（Agent 看到最新本地代码）
            if name in _MUTATING_TOOLS and workspace_id:
                await self._service.sync_local_to_sandbox(user_id, workspace_id, sandbox)
            # git 工具需要工作区根作为 cwd，单独分发（其余工具走统一 handler）
            if name == "git":
                result = await self._op_git(sandbox, arguments, workspace_id, user_id)
            elif name == "git_push":
                # 推送不经过沙箱执行：凭据只在后端，这里只传上下文给注入的推送能力
                result = await self._op_git_push(
                    arguments, user_id, session_id, workspace_id
                )
            else:
                result = await handler(sandbox, arguments)
            # 审计：变更类工具都会改动沙箱文件；run_command 还不受路径白名单约束，
            # 因此把上下文 + 参数摘要落日志，便于事后追查越界写入（不记文件正文）
            if name in _MUTATING_TOOLS:
                logger.info(
                    "sandbox_mutation tool=%s user=%s session=%s workspace=%s args=%s",
                    name,
                    user_id,
                    session_id,
                    workspace_id,
                    _audit_digest(arguments),
                )
            # Local 后端：write_file 同时写回本地源，避免 Agent 修改只停留在沙箱副本（双副本分裂）
            if name == "sandbox_write_file" and workspace_id:
                await self._service.write_local_workspace_file(
                    user_id, arguments["path"], arguments["content"]
                )
            # B2：写类工具执行后，把沙箱中与本地不同的文件拉回本地（覆盖 run_command 盲区）
            if name in _MUTATING_TOOLS and workspace_id:
                await self._service.sync_sandbox_to_local(user_id, workspace_id, sandbox)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 工具错误转为结果返回（业务错误 → command_error，系统故障 → tool_failure）
            return tool_error_result(name, exc)

    def _complete_arguments(
        self, name: str, arguments: dict, workspace_id: str
    ) -> dict:
        """按工具类型补全路径字段，返回新 dict（不污染调用方）。

        本次新增：sandbox_list_files/read/write/mkdir/delete 的 path、
        sandbox_move_path 的 from/to，统一做工作区路径自动补全。
        """
        args = dict(arguments)
        if name == "sandbox_move_path":
            for key in ("from", "to"):
                if key in args:
                    args[key] = _complete_workspace_path(args[key], workspace_id)
        else:
            args["path"] = _complete_workspace_path(args.get("path", ""), workspace_id)
        return args

    # ---- 文件操作 ----

    async def _op_sandbox_list_files(self, sandbox: AsyncSandbox, args: dict) -> dict:
        directory = workspace_path(args.get("path", ""), allow_root=True)
        depth = args.get("depth") or 1
        entries = await sandbox.files.list(directory, depth=depth)
        return {"path": relative_path(directory),
                "entries": [{"name": e.name, "path": relative_path(e.path),
                             "type": "directory" if e.type == FileType.DIR else "file",
                             "size": e.size or 0} for e in entries]}

    async def _op_sandbox_read_file(self, sandbox: AsyncSandbox, args: dict) -> dict:
        file_path = workspace_path(args["path"], allow_root=False)
        start_line = args.get("offset") or 1  # offset 语义：起始行号（1-based）
        if start_line < 1:
            raise ValueError("file offset (start line) must be >= 1")
        limit = args.get("limit") or DEFAULT_READ_LINES  # limit 语义：读取行数
        if limit < 1:
            raise ValueError("file limit (line count) must be positive")
        try:
            selected, total_lines, end_line = await _read_file_slice(
                sandbox, file_path, start_line, limit
            )
        except FileNotFoundError:
            # 路径不存在：附上父目录实际内容，帮助模型一眼看到正确路径、纠正幻觉
            parent = posixpath.dirname(file_path)
            names = await _list_dir_names(sandbox, parent)
            detail = (
                f"；{relative_path(parent)} 下有: {', '.join(names)}"
                if names
                else f"；{relative_path(parent)} 为空或不存在"
            )
            raise FileNotFoundError(f"路径 {args['path']} 不存在{detail}") from None

        if start_line > total_lines:
            return {
                "path": relative_path(file_path),
                "content": "",
                "totalLines": total_lines,
                "startLine": start_line,
                "endLine": total_lines,
                "omittedBefore": total_lines,
                "omittedAfter": 0,
                "nextSuggestion": f"起始行 {start_line} 已超过文件总行数 {total_lines}",
                "truncated": False,
            }

        # 信封：告诉模型文件总行数、本次读了哪些行、前后省略了多少、下一步该读哪
        numbered = "\n".join(
            f"{start_line + i:>5}│ {line}" for i, line in enumerate(selected)
        )
        omitted_before = start_line - 1
        omitted_after = total_lines - end_line

        if omitted_after > 0:
            next_start = end_line + 1
            next_end = min(end_line + limit, total_lines)
            suggestion = (
                f"文件共 {total_lines} 行，已读 {start_line}-{end_line}，"
                f"剩余 {omitted_after} 行；建议下次读 {next_start}-{next_end}"
            )
        else:
            suggestion = ""

        return {
            "path": relative_path(file_path),
            "content": numbered,
            "totalLines": total_lines,
            "startLine": start_line,
            "endLine": end_line,
            "omittedBefore": omitted_before,
            "omittedAfter": omitted_after,
            "nextSuggestion": suggestion,
            "truncated": omitted_before > 0 or omitted_after > 0,
        }

    async def _op_sandbox_write_file(self, sandbox: AsyncSandbox, args: dict) -> dict:
        file_path = workspace_path(args["path"], allow_root=False)
        await sandbox.files.write(file_path, args["content"])
        return {"path": relative_path(file_path), "message": "file saved"}

    async def _op_sandbox_make_directory(self, sandbox: AsyncSandbox, args: dict) -> dict:
        directory = workspace_path(args["path"], allow_root=False)
        await sandbox.files.make_dir(directory)
        return {"path": relative_path(directory), "message": "directory created"}

    async def _op_sandbox_move_path(self, sandbox: AsyncSandbox, args: dict) -> dict:
        src = workspace_path(args["from"], allow_root=False)
        dst = workspace_path(args["to"], allow_root=False)
        await sandbox.files.rename(src, dst)
        return {"path": relative_path(dst), "message": "path moved"}

    async def _op_sandbox_delete_path(self, sandbox: AsyncSandbox, args: dict) -> dict:
        file_path = workspace_path(args["path"], allow_root=False)
        await sandbox.files.remove(file_path)
        # 回收空父目录：只删子目录时父目录会留成空壳（列表里名字还在），
        # 极易被误判为「没删掉」。上溯到 workspace/{id} 根为止，非空自然停止。
        reclaimed = await _prune_empty_parents(sandbox, file_path)
        result = {"path": relative_path(file_path), "message": "path deleted"}
        if reclaimed:
            result["reclaimedEmptyDirs"] = reclaimed
        return result

    # ---- 进程操作 ----

    async def _op_sandbox_run_command(self, sandbox: AsyncSandbox, args: dict) -> dict:
        command = (args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        # git 统一走 git 工具（单一卡点：命令白名单/SSRF/输出脱敏/未来凭据都在那边）。
        # run_command 里显式拦截 git，避免模型绕开白名单直接 `git push` 等。
        # 诚实边界：这只是"引导 + 纵深防御"，`bash -c "git ..."` 仍能绕过；
        # 真正的边界是 M3 的 runner 沙箱（Agent 沙箱内不注入凭据）。
        program = command.split()[0] if command.split() else ""
        if program and program.rstrip("/").rsplit("/", 1)[-1] in ("git", "git.exe"):
            return {
                "error": "git 请使用 git 工具（受命令白名单 / SSRF / 输出脱敏约束），不要用 run_command 直接执行。",
                "kind": COMMAND_ERROR,
                "retryable": True,
                "suggestion": "改用 git 工具，subcommand 可取 status / log / diff / clone 等只读子命令。",
            }
        timeout = args.get("timeoutMs") or DEFAULT_COMMAND_TIMEOUT_MS
        if timeout < 1:
            raise ValueError("command timeout must be positive")
        try:
            result = await sandbox.commands.run(
                _join_argv(command, args.get("args")),
                envs=args.get("envs"),
                cwd=WORKSPACE.work_path(),
                timeout=timeout / 1000,
            )
        except CommandExitException as exc:
            # 非零退出是命令的**业务结果**（git 非仓库=128、cat 找不到文件等），
            # 不是系统故障。E2B 的 commands.run 会把非零退出抛成异常，这里转回结果，
            # 否则会被 tool_error_result 误判为 tool_failure（retryable=False）并触发熔断。
            result = exc
        stdout, stdout_truncated = _shorten(result.stdout or "")
        stderr, stderr_truncated = _shorten(result.stderr or "")
        resp = {"exitCode": result.exit_code or 0, "stdout": stdout, "stderr": stderr,
                "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated,
                # 显式标记：本工具不受工作区路径白名单约束（与文件类工具不同），
                # 让模型/UI/日志都能看出"这次调用可能碰到工作区之外的路径"
                "sandboxScope": "unrestricted"}
        # 命令失败（非零退出）或 stderr 含业务错误（伪成功，如 cat 找不到文件）→ retryable，交给模型自救
        suggestion = command_stderr_hint(stderr)
        if resp["exitCode"] != 0 or suggestion:
            resp["kind"] = COMMAND_ERROR
            resp["retryable"] = True
            if suggestion:
                resp["suggestion"] = suggestion
        return resp

    async def _op_sandbox_start_process(self, sandbox: AsyncSandbox, args: dict) -> dict:
        command = (args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        handle = await sandbox.commands.run(
            _join_argv(command, args.get("args")),
            cwd=WORKSPACE.work_path(),
            background=True,
        )
        pid = handle.pid if hasattr(handle, "pid") else 0
        return {"pid": pid, "logPath": ""}

    async def _op_sandbox_list_processes(self, sandbox: AsyncSandbox, args: dict) -> dict:
        processes = await sandbox.commands.list()
        return {"processes": [{"pid": p.pid, "command": p.cmd, "args": p.args or [],
                               "cwd": relative_path(p.cwd) if p.cwd and p.cwd.startswith(WORKSPACE.root) else (p.cwd or ""),
                               "tag": p.tag or ""} for p in processes]}

    async def _op_sandbox_kill_process(self, sandbox: AsyncSandbox, args: dict) -> dict:
        pid = args.get("pid") or 0
        if pid == 0:
            raise ValueError("process ID is required")
        await sandbox.commands.kill(pid)
        return {"pid": pid, "message": "process killed"}

    # ---- git（只读 + clone + 本地写，M2/M3）----

    async def _op_git(
        self, sandbox: AsyncSandbox, args: dict, workspace_id: str, user_id: str = ""
    ) -> dict:
        """执行 git 命令：白名单 + SSRF + 非交互 env + 提交身份 + 输出脱敏（见 ..gitops）。

        git 需要工作区根作为 cwd，故在 execute() 里单独分发并传入 workspace_id。
        远端写（push/pull）不在本工具开放：沙箱里没有凭据，推送走 git_push。
        """
        if not workspace_id:
            return {"error": "git 需要先选择一个工作区", "kind": COMMAND_ERROR,
                    "retryable": True, "suggestion": "请先在顶部选择工作区，再使用 git 工具。"}
        subcmd = (args.get("subcommand") or "").strip()
        argv = [str(a) for a in (args.get("args") or [])]
        identity = await self._resolve_git_identity(user_id)
        try:
            result = await run_git(
                sandbox,
                WORKSPACE.workspace_path(workspace_id),
                subcmd,
                argv,
                identity=identity,
            )
        except GitCommandRejected as exc:
            return {"error": str(exc), "kind": COMMAND_ERROR, "retryable": True,
                    "suggestion": ("该 git 命令不在白名单内。当前支持：只读（status/log/diff/show/blame…）、"
                                   "clone、本地写（add 显式路径 / commit -m 消息）；"
                                   "推送到远端请改用 git_push 工具。")}
        except CloneUrlRejected as exc:
            return {"error": str(exc), "kind": COMMAND_ERROR, "retryable": True,
                    "suggestion": "clone URL 未通过安全校验：仅支持公网 https/ssh，内网/云元数据端点被拒绝。"}
        stdout, stdout_truncated = _shorten(result["stdout"])
        stderr, stderr_truncated = _shorten(result["stderr"])
        resp = {"exitCode": result["exitCode"], "stdout": stdout, "stderr": stderr,
                "stdoutTruncated": stdout_truncated, "stderrTruncated": stderr_truncated}
        if result["exitCode"] != 0:
            resp["kind"] = COMMAND_ERROR
            resp["retryable"] = True
        return resp

    async def _op_git_push(
        self, args: dict, user_id: str, session_id: str, workspace_id: str
    ) -> dict:
        """把已提交的改动推送到工作区绑定的远端（不在沙箱里执行）。

        编排在 gitops.orchestrator：沙箱只 `git bundle create`，后端读出 bundle 后
        带凭据 push —— 因此 sandbox 内永远拿不到 token，模型的 prompt injection
        也偷不到凭据。HTTP 的「推送到 GitHub」按钮走的是同一份实现。
        """
        if not workspace_id:
            return {"error": "git_push 需要先选择一个工作区", "kind": COMMAND_ERROR,
                    "retryable": False,
                    "suggestion": "请先在顶部选择工作区，再让 Agent 推送。"}
        if self._push_workspace is None:
            return {"error": "推送能力未注入", "kind": COMMAND_ERROR, "retryable": False,
                    "suggestion": "后端组合根未装配 WorkspacePush，请检查 main.py。"}
        branch = str(args.get("branch") or "").strip()
        remote_branch = str(args.get("remoteBranch") or "").strip()
        try:
            result = await self._push_workspace(
                user_id, session_id, workspace_id, branch, remote_branch
            )
        except Exception as exc:
            # 只有推送编排的业务错误（PushRejected 带 code）转成结果返回；
            # 其它异常原样抛出，由 execute() 兜底成 tool_failure 并留日志 ——
            # 不能把代码 bug 伪装成「推送失败」让模型重试。
            if not hasattr(exc, "code"):
                raise
            return _push_error_payload(exc)
        return {"pushed": True, **result}

    async def _op_git_remote_branches(self, user_id: str, workspace_id: str) -> dict:
        """只读：远端分支列表 + 默认分支（带凭据，只在后端执行）。"""
        if not workspace_id:
            return {"error": "git_remote_branches 需要先选择一个工作区",
                    "kind": COMMAND_ERROR, "retryable": False,
                    "suggestion": "请先在顶部选择工作区。"}
        if self._remote_info is None:
            return {"error": "远端查询能力未注入", "kind": COMMAND_ERROR,
                    "retryable": False,
                    "suggestion": "后端组合根未装配 WorkspacePush，请检查 main.py。"}
        try:
            return await self._remote_info(user_id, workspace_id)
        except Exception as exc:
            if not hasattr(exc, "code"):
                raise
            return _push_error_payload(exc)


# 推送类错误码 → 给模型的「下一步」提示。按 code 查表，避免在 handler 里堆 if 分支。
_PUSH_ERROR_HINTS = {
    "push_non_fast_forward": (
        "远端该分支已有本地没有的提交（或历史完全无关），普通推送被拒，重试无用。"
        "两条合法路径（都不做强推）：① 换一个新分支推上去：git_push(remoteBranch='docs/xxx')，"
        "然后让用户在 GitHub 上开 PR；② 先与用户确认是否要基于远端分支重建提交"
        "（重新 clone 目标分支 → 把改动应用上去 → 再推）。"
        "不确定远端有哪些分支时先调 git_remote_branches。"
    ),
    "git_remote_missing": "让用户去「设置 → 工作区」填写该工作区的仓库 URL。",
    "git_credential_missing": "让用户去「设置」连接 GitHub（需要 repo 写权限的 PAT）。",
    "no_branch": "工作区可能还不是 git 仓库或还没有提交；先 git status 确认，必要时 git add/commit。",
    "sandbox_not_found": "让用户先在对话里让 Agent 运行一次，产生沙箱。",
    "workspace_not_found": "工作区不存在，请用户重新选择工作区。",
}


def _push_error_payload(exc: object) -> dict:
    """把推送类业务错误翻译成工具结果（含 code / retryable / 建议）。"""
    code = str(getattr(exc, "code", ""))
    retryable = bool(getattr(exc, "retryable", False))
    suggestion = _PUSH_ERROR_HINTS.get(code)
    if suggestion is None:
        suggestion = (
            "可读错误后重试一次（网络抖动可直接重试）。"
            if retryable
            else "这是需要用户先处理的配置/环境问题，不要重复调用；直接告诉用户去改什么。"
        )
    return {
        "error": str(exc),
        "code": code,
        "kind": COMMAND_ERROR,
        "retryable": retryable,
        "suggestion": suggestion,
    }


def _shorten(value: str) -> tuple[str, bool]:
    """超长输出做「保首保尾」投影：保留开头与结尾，中间省略（B4）。

    开头是命令起始输出，结尾往往是关键的错误/结果，两端比只保开头信息量更大。
    """
    if len(value) <= MAX_COMMAND_OUTPUT:
        return value, False
    half = MAX_COMMAND_OUTPUT // 2
    omitted = len(value) - 2 * half
    return f"{value[:half]}\n…（中间省略 {omitted} 字符）…\n{value[-half:]}", True
