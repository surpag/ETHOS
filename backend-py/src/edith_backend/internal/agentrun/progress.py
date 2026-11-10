"""运行中的进度落库（WAL 思路）。

**为什么需要它**：助手消息原来只在 run 收尾时写一次。于是 run 进行中刷新页面，
历史里只有用户那一句——推理、工具调用、正文全都不在，用户只能干等结束再刷新。
事件日志（`run_log.py`）能补发，但它活在进程内：后端重启过、多副本、或者前端
根本没拿到 pending 记录时，屏幕就是一片空白。

**它和工作日志的关系**：写的仍然是 `chat_messages` 里那条助手消息——同一个
`request_id`、同一行。终态写入会覆盖它，所以不存在第二份事实，也不需要和历史
对账。区别只是「什么时候写」：从「结束时一次」变成「跑的过程中持续写」。

**为什么不是每条事件都写**：sessionStorage 之外还要过 SQLite。正文增量是逐
token 的，逐条 commit 会把 IO 打满，还会让 WAL 无限增长。所以按时间窗合并
（默认 1s），并在工具结束这种「用户最想看到」的时刻强制落一次盘。

**顺序保证**：所有写入都串行（同一时刻最多一个 in-flight 写），`stop()` 会
等最后一个写完再返回。调用方必须在终态写入**之前** `await stop()`，否则一条
迟到的进度写会把刚补齐的耗时/证据/终止原因覆盖回 NULL。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..agentstream.types import Event
from ..session.store import SessionStore

logger = logging.getLogger(__name__)

# 进度落库的最小间隔。1 秒是「刷新后不至于整段消失」和「别把 SQLite 打满」的
# 折中：一次长任务几十秒，最多写几十次，代价可以忽略。
FLUSH_INTERVAL_SECONDS = 1.0

# 工具结果落库的长度上限，与终态写入保持一致（`execution.py` 里也是 2000）。
_RESULT_LIMIT = 2000


class ProgressRecorder:
    """把一个 run 的中间态写进它那条助手消息。"""

    def __init__(
        self,
        store: SessionStore,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        workspace_id: str | None = None,
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._session_id = session_id
        self._request_id = request_id
        self._workspace_id = workspace_id

        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tools: dict[str, dict[str, Any]] = {}

        self._inflight: asyncio.Task[None] | None = None
        self._dirty = False
        self._stopped = False
        self._last_flush = 0.0

    # ── 事件入口 ────────────────────────────────────────────────────────────

    def observe(self, event: Event) -> None:
        """喂一条事件。同步、不抛错——它挂在整个 run 的发射路径上。"""
        if self._stopped:
            return
        changed = False
        if event.type == "message.delta" and event.delta:
            self._content.append(event.delta)
            changed = True
        elif event.type == "reasoning.delta" and event.delta:
            self._reasoning.append(event.delta)
            changed = True
        elif event.type == "tool.started" and event.tool_call_id:
            self._tools[event.tool_call_id] = {
                "id": event.tool_call_id,
                "name": event.tool_name or "",
                "arguments": event.arguments or "",
                "status": event.tool_status or "",
                "result": "",
            }
            changed = True
        elif event.type == "tool.finished" and event.tool_call_id:
            record = self._tools.setdefault(
                event.tool_call_id,
                {
                    "id": event.tool_call_id,
                    "name": event.tool_name or "",
                    "arguments": "",
                    "status": "",
                    "result": "",
                },
            )
            if event.tool_name:
                record["name"] = event.tool_name
            if event.tool_status:
                record["status"] = event.tool_status
            if event.tool_result is not None:
                record["result"] = str(event.tool_result)[:_RESULT_LIMIT]
            changed = True

        if not changed:
            return
        # 工具结束是一次「可见的里程碑」：立刻落盘，让刷新后至少能看到这一步。
        self._schedule(force=event.type == "tool.finished")

    # ── 写入 ────────────────────────────────────────────────────────────────

    def _has_payload(self) -> bool:
        return bool(
            self._content or self._reasoning or self._tools
        )

    def _schedule(self, *, force: bool) -> None:
        if self._stopped or not self._has_payload():
            return
        now = time.monotonic()
        if self._inflight is not None:
            # 有一次写在跑：只标记「还有新内容」，等它落地后再补一次。
            self._dirty = True
            return
        if not force and now - self._last_flush < FLUSH_INTERVAL_SECONDS:
            return
        self._last_flush = now
        self._inflight = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            content = "".join(self._content)
            reasoning = "".join(self._reasoning)
            tools = list(self._tools.values())
            if content or reasoning or tools:
                await self._store.add_assistant_message(
                    self._user_id,
                    self._session_id,
                    content,
                    reasoning,
                    self._workspace_id,
                    tools,
                    None,  # 耗时只有收尾时才知道
                    None,  # 证据同理
                    None,  # end_reason 不写：NULL 表示「还没有结论」，UI 不会误标完成
                    self._request_id,
                )
        except Exception as exc:  # noqa: BLE001 - 进度写失败不能打断 run
            # 进度写失败只会让「刷新后的中间态」少一段，不该影响正在跑的回复。
            logger.warning(
                "progress flush failed request_id=%s: %s", self._request_id, exc
            )
        finally:
            self._inflight = None
            if self._dirty and not self._stopped:
                self._dirty = False
                self._schedule(force=True)

    async def stop(self) -> None:
        """停止接收新事件，并等最后一次写入落地。

        必须在终态写入之前调用：否则迟到的进度写会用「没有结论」的版本覆盖刚
        写好的耗时/证据/终止原因。
        """
        self._stopped = True
        inflight = self._inflight
        if inflight is not None:
            # 写任务的异常已经在 _flush 里吞掉并记了日志；这里用 gather 是为了
            # 不把「写进度失败」升级成「整个 run 失败」。
            await asyncio.gather(inflight, return_exceptions=True)
