"""usage 记录器：AgentRun 接受、完成或失败时写入用量记录。

对应 Go 版 internal/usage/recorder.go。
"""

from __future__ import annotations

from .store import RunAlreadyExists, RunNotFound, Store
from .types import Run, Summary, Tokens


class Recorder:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def start(self, run: Run) -> None:
        """记录一个已接受的运行；重复 requestID 抛 RunAlreadyExists。"""
        await self._store.start(run)

    async def finish(self, run: Run, tokens: Tokens) -> Summary:
        """写入完成运行的用量，并返回该会话新的用量汇总。"""
        await self._store.finish(run, tokens)
        return await self._store.session_summary(run.user_id, run.session_id)

    async def add_auxiliary(self, run: Run, tokens: Tokens) -> Summary:
        """把收尾之后发生的附属调用（自动记忆抽取）计入本次运行。

        返回刷新后的会话汇总——调用方据此推一条事件给前端，保证「实时看到的用量」
        和「刷新后的用量」是同一个数（只用 DB 里的值，不在前端重算）。
        """
        await self._store.add_auxiliary(run.request_id, tokens)
        return await self._store.session_summary(run.user_id, run.session_id)

    async def fail(self, request_id: str) -> None:
        """标记未完成运行失败；失败运行不计入会话用量。"""
        await self._store.fail(request_id)


__all__ = ["Recorder", "RunAlreadyExists", "RunNotFound"]
