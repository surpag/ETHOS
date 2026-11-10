"""一次 AgentRun 的事件日志：实时扇出 + 可重放。

## 为什么不是 asyncio.Queue

原来的实现是「一个 run 一个 Queue」——谁连上谁读，读走即消失。这在两种情况下
会丢事件：

1. 用户刷新页面：HTTP 连接断开，消费者消失。后台的 task 还在 `put_nowait`，
   但没人取，那些事件永久丢失。于是前端只能显示「任务仍在后台执行」，等 run
   结束再从数据库历史里把整轮补出来——这正是「刷新后看不到执行过程」的根因。
2. 用户在弱网下断流重连：同上。

Queue 是**管道**语义（一次性，谁读谁得），这里需要的是**日志**语义
（持久、可重放、多订阅者，各自记录读到哪）。换成日志之后，刷新页面 = 用
`after=<已收到的最大 seq>` 重新订阅，先补发错过的部分，再无缝接上实时。

## 要不要落库

不落。这里刻意只做**进程内**，理由：

- 权威事实已经有三份：`chat_messages`（历史）、`traces`（诊断）、`run_plans`
  （计划）。事件日志是它们的**派生视图**，用于实时渲染。再落一张表就是第四份
  事实，必然与前三份漂移，还得写对账逻辑。
- 断流重连这个场景里后端进程还活着，内存足够。
- 真正需要跨进程的场景（后端重启、多副本）应当接 Redis Stream / Kafka 一类
  的流存储，而不是塞进业务表。那时这里是唯一要换的边界。

代价必须显式承认：**后端进程重启 → 日志丢失 → 降级为「等结束拉历史」**，
和改造前一样。`self.recent` 的 TTL 到期后同理。

## 容量

单 run 的事件量以 content delta 为主，一个长任务几千条、几百 KB 量级。设上限
（条数 + 字符）作为兜底，超出时丢最老的并在补发前告知前端「断层」（`run.log_truncated`），
让前端退回「拉历史对齐」。宁可知情地降级，不要静默地少一段。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator

from ..agentstream.types import Event

# 单 run 日志上限：条数与字符数双限。取「一个很长的 run 也装得下」的量级，
# 真正的目的是防止某个失控 run 把进程内存吃穿。
MAX_EVENTS = 20_000
MAX_CHARS = 8_000_000

# 日志里丢弃的描述（不进 messages，仅供诊断）
TRUNCATED_EVENT = "run.log_truncated"


class RunEventLog:
    """一个 run 的事件序列：可多订阅者，可按 seq 重放。"""

    def __init__(self, request_id: str = "") -> None:
        self.request_id = request_id
        self._events: deque[Event] = deque()
        self._chars = 0
        self._seq = 0
        # 已被丢弃的最老事件序号。订阅者的 after 小于它就说明中间有断层。
        self.dropped_before = 0
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._closed = False

    # ---------- 生产端 ----------

    def publish(self, event: Event) -> None:
        """追加一条事件并扇出给当前所有订阅者。

        seq 由这里赋值，不由调用方给：事件的顺序只有一个权威来源，
        否则并发分支各自编号必然错序。
        """
        if self._closed:
            # 关闭后还有事件 = 有代码路径在 `close()` 之后发事件。
            # 丢弃并留痕：这不是致命错误，但意味着有收尾顺序问题。
            return

        self._seq += 1
        stamped = event.model_copy(update={"seq": self._seq})
        self._events.append(stamped)
        self._chars += _weight(stamped)
        self._evict()
        for queue in self._subscribers:
            queue.put_nowait(stamped)

    def close(self) -> None:
        """结束流：给所有订阅者投 None 哨兵，之后不再接受事件。"""
        if self._closed:
            return
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(None)
        self._subscribers.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------- 消费端 ----------

    async def open(self, after: int = 0) -> AsyncIterator[Event | None]:
        """从 seq > after 处开始读，读完历史后接上实时；末尾发一次 None。

        顺序很重要：**先注册订阅者，再读历史**。反过来会有窗口——读到历史末尾
        与注册订阅之间产生的事件谁也不会收到，表现为「重连后卡住不动」。
        先订阅则会重复投递历史区间内的事件，用 `last` 去重即可。
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            if after < self.dropped_before:
                # 断层：用户在读之前的那一段已经因为超限被丢弃。
                # 显式告知，让上层退回「拉历史」，而不是假装连续。
                yield Event(
                    type=TRUNCATED_EVENT,
                    request_id=self.request_id or None,
                    seq=after + 1,
                    delta=str(self.dropped_before),
                )

            last = after
            for event in list(self._events):
                if (event.seq or 0) <= after:
                    continue
                last = event.seq or last
                yield event

            if self._closed:
                yield None
                return

            while True:
                event = await queue.get()
                if event is None:
                    yield None
                    return
                if (event.seq or 0) <= last:
                    continue  # 历史段与实时段重叠，已发过
                last = event.seq or last
                yield event
        finally:
            self._subscribers.discard(queue)

    def backlog(self, after: int = 0) -> list[Event]:
        """同步取历史（给测试与非流式调用方）。"""
        return [e for e in self._events if (e.seq or 0) > after]

    @property
    def last_seq(self) -> int:
        return self._seq

    # ---------- 内部 ----------

    def _evict(self) -> None:
        while len(self._events) > MAX_EVENTS or self._chars > MAX_CHARS:
            if len(self._events) <= 1:
                # 单条就超限（异常巨大的工具结果）：留着这条，别再往下砍。
                break
            dropped = self._events.popleft()
            self._chars -= _weight(dropped)
            self.dropped_before = max(self.dropped_before, dropped.seq or 0)


def _weight(event: Event) -> int:
    """粗估一条事件的内存占用：只算大字段，够用来做容量守卫。"""
    return len(event.delta or "") + len(event.tool_result or "") + len(event.arguments or "") + 256
