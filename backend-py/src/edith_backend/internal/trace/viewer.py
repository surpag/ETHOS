"""trace 的命令行查看器。

    python -m edith_backend.internal.trace.viewer list  [--user U] [--limit 20] [--running]
    python -m edith_backend.internal.trace.viewer show  <request_id>
    python -m edith_backend.internal.trace.viewer slow  [--days 7] [--min-ms 5000]

只读：直接打开 SQLite 文件，不需要后端在跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import aiosqlite

from ... import config
from .store import Store
from .types import ERROR, OK, RUNNING, STATUS_LABELS

_MARKS = {OK: "✓", ERROR: "✗", RUNNING: "…"}

# metadata 展示优先级（排在前面的先显示）
_META_ORDER = (
    "model",
    "round",
    "attempt",
    "ttft_ms",
    "create_ms",
    "tokens",
    "tools",
    "messages",
    "msgs",
    "cached",
    "triggered",
    "summary_len",
    "attempts",
    "source",
    "resumed",
)


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms // 60000)}m{int(ms % 60000 // 1000)}s"


def _fmt_elapsed(started_at: str) -> str:
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(started_at)
    except ValueError:
        return "-"
    return _fmt_ms(delta.total_seconds() * 1000)


def _meta_text(raw: str | None, limit: int = 6) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    keys = [k for k in _META_ORDER if k in data]
    keys += [k for k in data if k not in keys]
    parts = []
    for key in keys[:limit]:
        value = data[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value)
        if len(text) > 60:
            text = text[:60] + "…"
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _status_text(trace: dict) -> str:
    status = trace.get("status") or RUNNING
    label = STATUS_LABELS.get(status, status)
    if status == RUNNING and not trace.get("finished_at"):
        return f"{label}（已 {_fmt_elapsed(trace.get('started_at', ''))} 未收尾）"
    reason = trace.get("end_reason")
    return f"{label} ({reason})" if reason else label


def _print_steps(steps: list[dict]) -> None:
    print("\n─── Steps ───")
    if not steps:
        print("  (无步骤)")
        return
    width = max(len(str(s["seq"])) for s in steps)
    for s in steps:
        mark = _MARKS.get(s.get("status") or OK, "?")
        indent = "  " + "  " * int(s.get("depth") or 0)
        meta = _meta_text(s.get("metadata"))
        line = (
            f"{indent}[{str(s['seq']).rjust(width)}] {mark} "
            f"{s['type']:<9}| {s['name'][:26]:<26}| "
            f"{_fmt_ms(s.get('duration_ms')).rjust(7)}"
        )
        if meta:
            line += f"  {meta}"
        print(line)
        if s.get("status") == ERROR and s.get("error_message"):
            print(f"{indent}      ↳ {s.get('error_type')}: {s['error_message'][:160]}")


def _print_trace(trace: dict, steps: list[dict]) -> None:
    print(f"═══ request_id={trace['request_id']} ═══")
    print(
        f"用户 {trace.get('user_id')} | 会话 {trace.get('session_id')} | "
        f"工作区 {trace.get('workspace_id') or '-'}"
    )
    print(f"模型 {trace.get('model_id') or '-'} | 状态 {_status_text(trace)}")
    print(
        f"起止 {trace.get('started_at')} → {trace.get('finished_at') or '（未收尾）'}"
    )
    wall = trace.get("duration_ms")
    self_ms = trace.get("self_time_ms")
    if wall is not None and self_ms is not None:
        top = sum(1 for s in steps if s.get("parent_seq") is None)
        print(
            f"墙钟 {_fmt_ms(wall)} | 自耗时 {_fmt_ms(self_ms)}（顶层 {top} 步）"
            f" | 未打点 {_fmt_ms(max(wall - self_ms, 0))}"
        )
    print(
        f"tokens {trace.get('total_tokens') if trace.get('total_tokens') is not None else '-'}"
        f" | 工具 {trace.get('tools_called') if trace.get('tools_called') is not None else '-'} 次"
        f" | 降级 {'是' if trace.get('degraded') else '否'}"
    )
    if trace.get("degradations"):
        try:
            for item in json.loads(trace["degradations"]):
                reason = f"（{item.get('reason')}）" if item.get("reason") else ""
                print(f"  ↳ {item.get('kind')}: {item.get('from')} → {item.get('to')}{reason}")
        except json.JSONDecodeError:
            pass
    if trace.get("user_input"):
        print(f"输入 {trace['user_input'][:160]}")
    if trace.get("error_message"):
        print(f"\n异常 {trace.get('error_type')}: {trace.get('error_message')}")
    _print_steps(steps)
    if trace.get("error_traceback"):
        print("\n─── Traceback ───")
        print(trace["error_traceback"].rstrip())


async def _cmd_list(store: Store, args: argparse.Namespace) -> None:
    traces = await store.list_traces(
        user_id=args.user, limit=args.limit, running_only=args.running
    )
    if not traces:
        print("（没有匹配的 trace。确认 EDITH_DB_PATH 指向正确的库。）")
        return
    print(
        f"{'状态'.ljust(22)} {'起止'.ljust(17)} {'墙钟'.rjust(8)} "
        f"{'tokens'.rjust(7)} {'工具'.rjust(4)}  {'模型':<16} 请求ID"
    )
    for t in traces:
        mark = _MARKS.get(t.get("status") or RUNNING, "?")
        status = f"{mark}{_status_text(t)}"
        print(
            f"{status[:22].ljust(22)} {(t.get('started_at') or '')[:17].ljust(17)} "
            f"{_fmt_ms(t.get('duration_ms')).rjust(8)} "
            f"{str(t.get('total_tokens') if t.get('total_tokens') is not None else '-').rjust(7)} "
            f"{str(t.get('tools_called') if t.get('tools_called') is not None else '-').rjust(4)}  "
            f"{(t.get('model_id') or '-')[:16]:<16} {t['request_id']}"
        )


async def _cmd_show(store: Store, args: argparse.Namespace) -> None:
    trace = await store.get_trace(args.request_id)
    if trace is None:
        print(f"找不到 trace: {args.request_id}")
        sys.exit(1)
    _print_trace(trace, await store.get_steps(args.request_id))


async def _cmd_slow(store: Store, args: argparse.Namespace) -> None:
    since = (
        datetime.now().astimezone() - timedelta(days=args.days)
    ).isoformat(timespec="seconds")
    rows = await store.slow_steps(since, args.min_ms, args.limit)
    print(f"─── 最慢步骤（近 {args.days} 天，≥{_fmt_ms(args.min_ms)}）───")
    if not rows:
        print("（无。样本不足或阈值过高。）")
    for r in rows:
        print(
            f"{_fmt_ms(r.get('duration_ms')).rjust(8)}  {r['type']:<9}| "
            f"{r['name'][:26]:<26}| {(r['started_at'] or '')[:17]} | {r['request_id']}"
        )
    stats = await store.step_stats(since)
    print(f"\n─── 按环节聚合（近 {args.days} 天）───")
    if not stats:
        print("（无数据）")
        return
    print(f"{'平均'.rjust(8)} {'最慢'.rjust(8)} {'次数'.rjust(5)} {'失败'.rjust(4)}  环节")
    for s in stats:
        print(
            f"{_fmt_ms(s.get('avg_ms')).rjust(8)} {_fmt_ms(s.get('max_ms')).rjust(8)} "
            f"{str(s.get('calls')).rjust(5)} {str(s.get('errors')).rjust(4)}  "
            f"{s['type']}:{s['name']}"
        )


async def _run(args: argparse.Namespace) -> None:
    config.load_environment()
    path = args.db or config.database_path()
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 5000")
    try:
        store = Store(db)
        if args.command == "list":
            await _cmd_list(store, args)
        elif args.command == "show":
            await _cmd_show(store, args)
        else:
            await _cmd_slow(store, args)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="edith-trace", description="EDITH 链路追踪查看器"
    )
    parser.add_argument("--db", help="SQLite 路径（默认取 EDITH_DB_PATH）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出最近的 run")
    p_list.add_argument("--user", help="按 user_id 过滤")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--running", action="store_true", help="只看未收尾（可能卡住）")

    p_show = sub.add_parser("show", help="查看单次 run 的步骤树")
    p_show.add_argument("request_id")

    p_slow = sub.add_parser("slow", help="最慢步骤与环节聚合")
    p_slow.add_argument("--days", type=int, default=7)
    p_slow.add_argument("--min-ms", type=float, default=5000)
    p_slow.add_argument("--limit", type=int, default=30)

    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except sqlite3.OperationalError as exc:
        print(f"读取失败（trace 表可能尚未创建，先启动一次后端即可）：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
