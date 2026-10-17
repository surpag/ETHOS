"""阶段 8 验收测试：上传文件处理与 MCP 运行时。"""

from __future__ import annotations

import asyncio
import os

from edith_backend.internal.agentrun.configuration import (
    message_with_uploads,
    run_instruction,
)
from edith_backend.internal.skills.types import SkillSummary
from edith_backend.internal.userconfig.mcp import MCPServer
from edith_backend.internal.userconfig.mcp_runtime import MCPToolContext, _toolset_name
from edith_backend.internal.volume.trace import (
    token_fingerprint,
    trace_enabled,
    user_fingerprint,
)


def test_message_with_uploads_empty() -> None:
    assert message_with_uploads("你好", []) == "你好"


def test_message_with_uploads_appends_attachments() -> None:
    result = message_with_uploads("分析一下", ["uploads/report.pdf", "uploads/data.csv"])
    assert result.startswith("分析一下\n\n附件：")
    assert "`report.pdf`" in result
    assert "`data.csv`" in result


def test_message_with_uploads_only_files() -> None:
    result = message_with_uploads("", ["uploads/a.txt"])
    assert result == "附件：\n- `a.txt`"


def test_run_instruction_with_uploads() -> None:
    instruction = run_instruction([], "", ["uploads/a.txt"])
    assert "本次用户上传文件" in instruction
    assert "uploads/a.txt" in instruction
    assert "uploads/ 读取" in instruction


def test_run_instruction_skill_plus_uploads() -> None:
    summaries = [SkillSummary(name="current-time", description="获取当前时间")]
    instruction = run_instruction(summaries, "", ["uploads/a.txt"])
    assert "current-time" in instruction
    assert "本次用户上传文件" in instruction


def test_run_instruction_no_uploads() -> None:
    assert run_instruction([], "", []) == ""


def test_mcp_toolset_name_normalization() -> None:
    server = MCPServer(id="abc123-def456", name="Search Service", url="", transport="", enabled=True)
    name = _toolset_name(server)
    assert name.startswith("search_service_")
    assert name.endswith("abc123de")  # 去掉横杠，前 8 位


def test_mcp_toolset_name_chinese_falls_back() -> None:
    server = MCPServer(id="xyz789", name="搜索", url="", transport="", enabled=True)
    name = _toolset_name(server)
    assert name == "mcp_xyz789"


def test_mcp_tool_context_execute_unknown() -> None:
    context = MCPToolContext()
    result = asyncio.run(context.execute("no_such_tool", {}))
    assert "unknown MCP tool" in result


def test_user_fingerprint_deterministic() -> None:
    assert user_fingerprint("user_abc") == user_fingerprint("user_abc")
    assert len(user_fingerprint("user_abc")) == 12


def test_user_fingerprint_hides_plaintext() -> None:
    assert user_fingerprint("user_3Hm1pmVlyWo1rW9BYe0kY6ja3DM") != "user_3Hm1pmVlyWo1rW9BYe0kY6ja3DM"


def test_token_fingerprint_hex() -> None:
    digest = token_fingerprint("secret-token")
    assert len(digest) == 64
    assert digest != "secret-token"


def test_trace_enabled_default_false() -> None:
    os.environ.pop("EDITH_E2B_DEBUG_HTTP", None)
    assert trace_enabled() is False


