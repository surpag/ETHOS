"""工具结果的「定型」：只发生一次，在结果进入模型历史之前。

## 为什么必须有这一步

先区分两件常被混为一谈的事：

1. **工具结果的内容是动态的** —— 同一个 `read_file` 不同时刻读到不同内容，这很正常。
2. **已经进入对话历史的工具结果是否可变** —— 一次调用返回并写进 messages 之后，
   后续轮次是否回头改写它。

前缀缓存只关心第 2 件事：命中要求「从第一个 token 起逐字节一致」。历史里任何一条
旧消息被原地改写，从改写位置开始的全部缓存都作废。所以真正要保证的不是「工具结果
固定」，而是**每一次工具调用的结果在进入历史时确定下来，之后不再变化**。

## 实测依据

ETHOS 侧 18 轮工具循环的 per-call 埋点：稳态命中率 ≈99%，唯独出现 `trimmed=1`
的轮次掉到 43.2% / 61.4%。也就是说，run 内唯一让前缀缓存失效的动作，是我们自己
逐轮对旧工具结果做的截断，而不是模型行为。

## 因此的取舍

- 预算控制前移到「结果诞生时」，一次定型，写进 messages 的内容此后不可变；
- 旧的逐轮截断下线，只保留**只读审计**（看有没有路径绕过定型），不再改写历史；
- 文件/终端状态变了不靠改写历史表达，而是**重新调用工具、追加一条新结果**——
  历史里同时存在「当时看到的」和「现在看到的」，这才是正确的事实记录；
- 历史整体过长交给**显式压缩**（summary_compress / run 内 compact），
  那是一次整段替换，代价明确、可观测，而不是每轮悄悄削短。

## 定型规则（不按工具名分支）

按**内容形状**决定裁剪方式，而不是维护一张工具名白名单——MCP 工具的名字与能力
都是运行时才知道的，按名字分支必然有漏网的：

- JSON 文档：保留原始开头 + **顶层结构骨架**（标量字段全留，容器只报形状）；
- 纯文本：保头 + 保尾（命令输出、traceback、diff 的关键结论常在末尾）；
- 低于预算：原样保留，不做任何填充（100 字符的结果就存 100 字符）。

裁剪时**必须留下可执行的续读指引**，否则模型会以为被裁掉的部分是空的。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

# 单次工具结果进入模型历史的预算（字符）。默认 12000 字符 ≈ 3K~4K token，
# 约占 128K 窗口的 3%：即使一轮并发多个工具也不会挤爆预算。
# 定得太小会让模型反复重读同一区域（每次都花一次沙箱往返），得不偿失。
DEFAULT_MAX_CHARS = 12000
# 错误结果预算更小：它通常短而关键，但 traceback 可能很长，仍需兜底。
DEFAULT_ERROR_MAX_CHARS = 6000
# 头尾都保留时，头部分到的比例（0.65 ≈ 头 2/3 尾 1/3）
HEAD_RATIO = 0.65
# JSON 骨架最多占预算的比例：骨架是「模型能不能继续读」的关键，优先保证；
# 但不能无限长（字段极多时），超了就只保前若干字段。
SKELETON_RATIO = 0.4

#: 定型标记。带版本号，且只在**长度关系也吻合**时才被当成「已定型」（见
#: `_already_materialized`）——单纯字符串匹配会让「读到含这行字的文件」被误判。
MATERIALIZE_MARK = "工具结果已裁剪"
MARK_VERSION = "v1"
#: 标记块的最大长度上界（用于幂等判定的长度守卫）。notice 正文约 200~300 字符，
#: 骨架另算，这里只用于判断「输出是否只是由定型产生的那点超出」。
_NOTICE_SLACK = 600
#: 固定提示语的字符成本（不含骨架），用于在 JSON 分支里给原文开头留够预算。
_FIXED_NOTICE_CHARS = 320


def _env_int(name: str, default: int) -> int:
    """读环境变量（每次调用时读，便于测试与运行时调整）。"""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_chars_for(is_error: bool = False) -> int:
    """当前生效的单条结果预算。"""
    if is_error:
        return _env_int("EDITH_TOOL_RESULT_ERROR_MAX_CHARS", DEFAULT_ERROR_MAX_CHARS)
    return _env_int("EDITH_TOOL_RESULT_MAX_CHARS", DEFAULT_MAX_CHARS)


@dataclass(frozen=True)
class Materialized:
    """定型后的工具结果：进入模型历史的那份确定内容。"""

    content: str
    original_chars: int
    strategy: str  # full | json_head | head_tail | already_materialized
    omitted_chars: int

    @property
    def truncated(self) -> bool:
        return self.omitted_chars > 0


def _looks_like_json_document(text: str) -> tuple[bool, Any]:
    """是否是完整的 JSON 对象/数组；是则一并返回解析结果。

    只用于选裁剪形状，不做业务判断：解析失败就走纯文本裁剪，不影响正确性。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False, None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, None
    return isinstance(parsed, (dict, list)), parsed


def _shape_label(value: Any) -> str:
    """容器的形状描述：只报类型与规模，不报内容。"""
    if isinstance(value, dict):
        return f"<对象, {len(value)} 个字段，正文已省略>"
    if isinstance(value, list):
        return f"<数组, {len(value)} 项，正文已省略>"
    return "<已省略>"


def _json_skeleton(parsed: Any, max_chars: int) -> str:
    """提取顶层结构骨架：标量字段全留，容器字段只留形状。

    为什么不按字段名挑（total / next_cursor / has_more）：那是某个工具的约定，
    换一个 MCP 工具就失效，而工具名和字段名都是运行时才知道的。按**值的类型**
    区分才是通用的——分页游标、总数、状态、错误码几乎总是标量，体积则在容器里。

    这一步解决的是「只保留开头会丢尾部关键字段」：`next_cursor` / `has_more`
    常常出现在 JSON 末尾，裁掉它们模型就不知道还有下一页，会直接宣称「已全部读取」。
    """
    if isinstance(parsed, list):
        text = f"顶层为数组，共 {len(parsed)} 项。"
    else:
        lines: list[str] = []
        for key, value in parsed.items():
            key_text = json.dumps(str(key), ensure_ascii=False)
            if isinstance(value, (dict, list)):
                lines.append(f'  {key_text}: "{_shape_label(value)}"')
            else:
                lines.append(
                    f"  {key_text}: {json.dumps(value, ensure_ascii=False, default=str)}"
                )
            if sum(len(line) for line in lines) > max_chars:
                lines.append(f"  …（其余 {len(parsed) - len(lines)} 个字段未列出）")
                break
        text = "{\n" + ",\n".join(lines) + "\n}"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（骨架过长，已截断）"
    return text


def _notice(
    original: int,
    kept: int,
    detail: str,
    *,
    skeleton: str = "",
    is_json: bool = False,
) -> str:
    """裁剪标记：既说明发生了什么，也说明**怎么拿回被省略的部分**。

    只写「已截断」是不够的：模型会当成「后面的内容不存在」或「文件就这么长」，
    于是不再续读，直接基于不完整事实下结论。
    """
    bar = "─" * 8
    head = (
        f"\n{bar} {MATERIALIZE_MARK} [{MARK_VERSION}] {bar}\n"
        f"原始 {original} 字符，本次呈现 {kept} 字符{detail}。\n"
    )
    if is_json:
        head += (
            "⚠️ **这是被截断的 JSON 片段，不是完整结果**。"
            "下面的顶层结构来自完整解析，其中的标量字段（如分页游标、总数、"
            "状态标志）是准确的：\n"
            f"{skeleton}\n"
        )
    return (
        head
        + "需要被省略的内容时，请缩小范围后重新调用（读文件用 offset/limit；"
        "命令输出用 head/tail，或先重定向到文件再用 sandbox_read_file 分段读取；"
        "结构化结果用分页游标/limit 参数）。\n"
        "不要假设被省略的部分为空或不存在。\n"
        f"{bar}────────────────{bar}"
    )


def _already_materialized(text: str, budget: int) -> bool:
    """这条内容是否已经由定型器产出过（幂等判定）。

    为什么不能只做字符串匹配：工具读到的文件正文完全可能包含这一行字——最典型
    的就是读这个模块自己的源码。那会被误判成「已定型」而跳过裁剪，让预算失效，
    而且失效是静默的。

    加上长度关系后，误判需要同时满足「含完整版本标记」和「长度刚好略超预算」
    两个条件：定型输出必然满足，随手读到一段含该字符串的正文几乎不可能满足。
    """
    if MATERIALIZE_MARK not in text:
        return False
    if not (budget < len(text) <= budget + _NOTICE_SLACK):
        return False
    return f"[{MARK_VERSION}]" in text


def materialize_tool_result(
    tool_name: str, result: str, *, is_error: bool = False
) -> Materialized:
    """把一次工具调用的原始结果定型为「模型可见版本」。

    纯函数：同一个输入永远得到同一个输出，且不修改入参。这是可复现的前提——
    结果一旦定型，无论后续发生什么（重试、续跑、压缩），这一段历史都逐字节稳定。

    `tool_name` 只用于日志归属，规则不按它分支（见模块文档）。
    """
    text = result if isinstance(result, str) else str(result or "")
    original = len(text)
    budget = max_chars_for(is_error)
    if _already_materialized(text, budget):
        return Materialized(text, original, "already_materialized", 0)
    if original <= budget:
        return Materialized(text, original, "full", 0)

    is_json, parsed = _looks_like_json_document(text)
    # 预算太小时骨架会成为负担（notice 本身就装不下）：此时退化成纯文本裁剪，
    # 宁可少给一点结构性信息，也不要产出比原文没小多少的「定型结果」。
    if is_json and budget >= 400:
        # 三块内容要分摊预算：原始开头、固定提示语、结构骨架。
        # 固定提示语（不含骨架）约 300 字符，是硬成本；原文开头至少留 30%，
        # 否则「保留原始开头」就名存实亡。剩下的才给骨架。
        min_head = max(1, int(budget * 0.3))
        skeleton_budget = min(
            max(0, int(budget * SKELETON_RATIO)),
            max(0, budget - min_head - _FIXED_NOTICE_CHARS),
        )
        skeleton = _json_skeleton(parsed, skeleton_budget) if skeleton_budget else ""
        probe = _notice(original, 0, "", skeleton=skeleton, is_json=True)
        head_len = max(min_head, budget - len(probe))
        head_len = min(head_len, original)
        head = text[:head_len]
        omitted = original - head_len
        notice = _notice(
            original,
            head_len,
            f"（保留原始开头 {head_len} 字符，末尾省略 {omitted} 字符）",
            skeleton=skeleton,
            is_json=True,
        )
        return Materialized(
            head + notice, original, "json_head", omitted
        )

    head_len = max(1, int(budget * HEAD_RATIO))
    tail_len = max(0, budget - head_len)
    head = text[:head_len]
    tail = text[original - tail_len :] if tail_len else ""
    omitted = original - head_len - tail_len
    notice = _notice(
        original,
        head_len + tail_len,
        f"（保留开头 {head_len} 与结尾 {tail_len} 字符，中间省略 {omitted} 字符）",
    )
    return Materialized(
        head + notice + tail, original, "head_tail", omitted
    )


def count_oversized_tool_results(messages: list[dict]) -> int:
    """统计历史里仍超过预算的 tool 消息数（只读审计，不改写）。

    正常路径下这个值恒为 0：结果在进入历史前就已定型。一旦非 0，说明有代码路径
    绕过了 `materialize_tool_result`（新增的工具结果注入点、恢复续跑回填的历史、
    外部写入的多模态块等）——这必须被看见，而不是靠逐轮截断悄悄兜住。
    """
    budget = max_chars_for(False)
    count = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= budget:
            continue
        # 已定型的结果天然比预算多一个标记块的长度，不算「绕过」。
        if MATERIALIZE_MARK in content:
            continue
        count += 1
    return count
