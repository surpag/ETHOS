"""tools 聚合器：汇总各模块的 Agent 工具。

对应 Go 版 internal/tools/registry.go。
"""

from __future__ import annotations

import json

# 工具错误分类：业务错误（模型可自救）与系统故障（重试无用、参与熔断）彻底分开。
TOOL_FAILURE = (
    "tool_failure"  # 系统故障：SDK 异常 / 连接断开 / 超时（重试无用，参与熔断）
)
COMMAND_ERROR = (
    "command_error"  # 业务错误：命令失败 / 路径错误 / 权限不足（模型可自救，不熔断）
)


def describe_tool_failure(tool_name: str, exc: Exception) -> str:
    """把工具系统故障包装成模型可理解、且明确提示「重试无用」的描述。"""
    return (
        f"工具 {tool_name} 执行时发生系统故障：{exc}。"
        "这不是你的命令或参数问题，重试无法解决，请停止调用该工具并向用户说明故障。"
    )


# 业务性异常 → retryable + 针对性 suggestion，引导模型自救（不触发熔断）。
# 每个异常类型配一条可执行的修正建议，让模型「看到错误就知道下一步该怎么做」。
_BUSINESS_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    (
        FileNotFoundError,
        "文件或目录不存在，请先用 sandbox_list_files 查看实际目录结构，再以正确路径重试。",
    ),
    (IsADirectoryError, "目标路径是目录而非文件，请核对目标类型后重试。"),
    (NotADirectoryError, "目标路径是文件而非目录，请核对目标类型后重试。"),
    (PermissionError, "权限不足，请检查目标路径的读写权限，或换用有权限的目录重试。"),
)


def tool_error_result(tool_name: str, exc: Exception) -> str:
    """把工具异常转成结构化错误结果 JSON。

    业务错误（文件/路径/权限）→ command_error + retryable=true + suggestion，交给模型自救；
    系统故障（超时/SDK 异常等）→ tool_failure + retryable=false，参与熔断统计。
    """
    for err_type, suggestion in _BUSINESS_ERRORS:
        if isinstance(exc, err_type):
            return json.dumps(
                {
                    "error": str(exc),
                    "kind": COMMAND_ERROR,
                    "retryable": True,
                    "suggestion": suggestion,
                },
                ensure_ascii=False,
            )
    return json.dumps(
        {
            "error": describe_tool_failure(tool_name, exc),
            "kind": TOOL_FAILURE,
            "retryable": False,
        },
        ensure_ascii=False,
    )


# 命令 stderr 常见业务错误关键词 → 针对性建议。
# 用于识别「exit_code=0 但 stderr 有错」的伪成功（如 cat 找不到文件），避免模型误判为成功。
_COMMAND_STDERR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "no such file or directory",
        "命令引用的文件或目录不存在，请先用 sandbox_list_files 确认实际路径后重试。",
    ),
    (
        "command not found",
        "命令不存在，请检查命令名是否拼写正确，或换用沙箱中已安装的命令。",
    ),
    (
        "permission denied",
        "权限不足，请检查目标路径的读写/执行权限，或换用有权限的目录。",
    ),
    ("not a directory", "目标路径不是目录，请核对目标类型后重试。"),
    ("is a directory", "目标路径是目录而非文件，请核对目标类型后重试。"),
    ("cannot access", "无法访问目标路径，请确认路径存在且可读后重试。"),
)


def command_stderr_hint(stderr: str) -> str | None:
    """从命令 stderr 识别业务错误，返回针对性建议；无命中返回 None。"""
    low = (stderr or "").lower()
    for keyword, suggestion in _COMMAND_STDERR_HINTS:
        if keyword in low:
            return suggestion
    return None


class ToolRegistry:
    """汇总多个工具集，统一提供工具定义与执行分发。"""

    def __init__(self, tool_sets: list) -> None:
        self._tool_sets = tool_sets
        self._by_name: dict[str, object] = {}
        for tool_set in tool_sets:
            for definition in tool_set.tool_definitions():
                self._by_name[definition["function"]["name"]] = tool_set

    def tool_definitions(self) -> list[dict]:
        return [d for tool_set in self._tool_sets for d in tool_set.tool_definitions()]

    async def execute(
        self,
        name: str,
        arguments: dict,
        user_id: str,
        session_id: str,
        workspace_id: str = "",
    ) -> str:
        """统一执行入口（本次修改：透传 workspace_id 给各工具集，供 Sandbox 工具路径自动补全）。"""
        tool_set = self._by_name.get(name)
        if tool_set is None:
            return json.dumps(
                {"error": f"unknown tool: {name}", "kind": TOOL_FAILURE},
                ensure_ascii=False,
            )
        try:
            return await tool_set.execute(
                name, arguments, user_id, session_id, workspace_id
            )  # 执行三个不同类型的tool，systemtool,sandbox,CronTool
        except (
            Exception
        ) as exc:  # noqa: BLE001 - 兜底：业务错误 → command_error，系统故障 → tool_failure
            return tool_error_result(name, exc)
