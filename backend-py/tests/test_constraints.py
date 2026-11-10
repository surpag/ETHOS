"""约束存储（constraints）的回归测试。

这是上下文工程里最容易做漏的一层：约束如果住在对话历史里，就会跟着摘要一起
被改写（"不要用 Go" → "讨论了语言选型"），而**改写即失效**。所以核心断言不是
"摘要保留得好"，而是"约束根本不经过摘要"。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite

from edith_backend.internal.agentrun.configuration import ConfiguredRun
from edith_backend.internal.agentrun.execution import summary_compress
from edith_backend.internal.session.constraint_store import (
    KIND_HARD,
    KIND_SOFT,
    Constraint,
    ConstraintStore,
    extract_constraints,
    render_constraints,
)
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.trace.tracer import new_tracer


async def _fresh() -> tuple[aiosqlite.Connection, SessionStore, ConstraintStore]:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    return db, SessionStore(db), ConstraintStore(db)


# ---------------------------------------------------------------- 规则抽取


def test_extract_splits_and_classifies() -> None:
    """一条消息里的多条约束要拆成原子项，并分清强弱。"""
    items = extract_constraints("必须使用 Python 3.11，不要动 migrations/ 目录。记住我习惯用 pnpm。")
    texts = [i.text for i in items]
    assert texts == [
        "必须使用 Python 3.11",
        "不要动 migrations/ 目录",
        "记住我习惯用 pnpm",
    ]
    assert [i.kind for i in items] == [KIND_HARD, KIND_HARD, KIND_SOFT]


def test_extract_ignores_plain_chat() -> None:
    """普通陈述/闲聊不该变成约束——滥收会稀释真正硬约束的注意力。"""
    assert extract_constraints("今天天气不错，我们看看这个项目。") == []
    assert extract_constraints("") == []
    # 太短（无信息量）与太长（整段需求）都跳过
    assert extract_constraints("必须") == []
    assert extract_constraints("必须" + "很长的描述" * 60) == []


def test_extract_ignores_one_off_instructions() -> None:
    """一次性指令不能变成长期约束——误收的代价是模型永远遵守一条早已失效的要求。"""
    # 约束对象是本轮 Agent 行为
    assert extract_constraints("只回复 ok 两个字母，不要调用任何工具。") == []
    assert extract_constraints("不要输出解释，直接给结论。") == []
    # 明确的一次性限定
    assert extract_constraints("这次先不要动 tests 目录。") == []
    assert extract_constraints("今天只跑单测，不跑全量。") == []


def test_extract_keeps_persistent_conventions() -> None:
    """有持久副词 / 对象是项目状态时仍要收——别把该留的也筛掉。"""
    assert [i.text for i in extract_constraints("以后每次调用工具前先读文件。")] == [
        "以后每次调用工具前先读文件"
    ]
    assert [i.text for i in extract_constraints("必须用 pnpm 管理依赖。")] == [
        "必须用 pnpm 管理依赖"
    ]


def test_extract_dedupes_within_message() -> None:
    items = extract_constraints("不要用 Go。不要用 Go。")
    assert len(items) == 1


# ---------------------------------------------------------------- 落库与作用域


def test_capture_dedupes_across_messages() -> None:
    """同一句话第二次说：不新增行（数据库层唯一索引保证，不靠应用层 if）。"""

    async def go() -> None:
        db, _store, constraints = await _fresh()
        try:
            first = await constraints.capture_message(
                "u1", session_id="s1", workspace_id=None, message="不要用 Go 语言。"
            )
            second = await constraints.capture_message(
                "u1", session_id="s1", workspace_id=None, message="不要用 Go 语言。"
            )
            assert len(first) == 1
            assert second == []
            active = await constraints.list_active("u1", session_id="s1")
            assert len(active) == 1
            assert active[0].text == "不要用 Go 语言"
        finally:
            await db.close()

    asyncio.run(go())


def test_capture_scope_workspace_vs_session() -> None:
    """有工作区挂工作区（跨会话复用），没有就挂会话——不双写。"""

    async def go() -> None:
        db, _store, constraints = await _fresh()
        try:
            await constraints.capture_message(
                "u1", session_id="s1", workspace_id="w1", message="必须用 pnpm。"
            )
            # 同工作区的另一个会话也能拿到（项目约定应跨会话生效）
            in_other_session = await constraints.list_active(
                "u1", session_id="s2", workspace_id="w1"
            )
            assert [c.text for c in in_other_session] == ["必须用 pnpm"]
            # 别的会话 + 别的工区拿不到
            assert await constraints.list_active("u1", session_id="s2") == []

            await constraints.capture_message(
                "u1", session_id="s1", workspace_id=None, message="记住我喜欢短回答。"
            )
            session_only = await constraints.list_active("u1", session_id="s1")
            assert [c.text for c in session_only] == ["记住我喜欢短回答"]
        finally:
            await db.close()

    asyncio.run(go())


def test_link_source_backfills_message_id() -> None:
    """抽取先于消息落库，溯源 id 必须事后回填（否则查不到「这话是谁说的」）。"""

    async def go() -> None:
        db, store, constraints = await _fresh()
        try:
            ids = await constraints.capture_message(
                "u1", session_id="s1", workspace_id=None, message="必须用 uv 管理依赖。"
            )
            assert ids
            message_id = await store.add_user_message("u1", "s1", "必须用 uv 管理依赖。")
            await constraints.link_source("u1", ids, message_id)

            cursor = await db.execute(
                "SELECT source_message_id FROM constraints WHERE id = ?", (ids[0],)
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row[0] == message_id
        finally:
            await db.close()

    asyncio.run(go())


def test_delete_session_removes_session_constraints_only() -> None:
    """删会话要带走会话级约束，但工作区级约束必须留下（跨会话复用）。"""

    async def go() -> None:
        db, store, constraints = await _fresh()
        try:
            await constraints.capture_message(
                "u1", session_id="s1", workspace_id=None, message="必须用 uv。"
            )
            await constraints.capture_message(
                "u1", session_id="s1", workspace_id="w1", message="禁止改 CI 配置。"
            )
            assert len(await constraints.list_active("u1", session_id="s1", workspace_id="w1")) == 2

            await store.delete_session("u1", "s1")

            cursor = await db.execute("SELECT scope, scope_id, text FROM constraints")
            rows = await cursor.fetchall()
            await cursor.close()
            assert [tuple(r) for r in rows] == [("workspace", "w1", "禁止改 CI 配置")]
        finally:
            await db.close()

    asyncio.run(go())


def test_revoke_and_supersede_keep_history() -> None:
    """撤销/取代都不物理删除——没有历史就说不清「当时为什么那么做」。"""

    async def go() -> None:
        db, _store, constraints = await _fresh()
        try:
            old = await constraints.add(
                "u1", scope="workspace", scope_id="w1", text="必须用 npm", kind=KIND_HARD
            )
            new = await constraints.add(
                "u1", scope="workspace", scope_id="w1", text="必须用 pnpm", kind=KIND_HARD
            )
            assert old and new
            assert await constraints.supersede("u1", old, new)
            active = await constraints.list_active("u1", workspace_id="w1")
            assert [c.text for c in active] == ["必须用 pnpm"]

            cursor = await db.execute(
                "SELECT status, superseded_by FROM constraints WHERE id = ?", (old,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row[0] == "superseded" and row[1] == new

            assert await constraints.revoke("u1", new)
            assert await constraints.list_active("u1", workspace_id="w1") == []
            # 撤销后可以重新加回同文（部分唯一索引只约束 active）
            again = await constraints.add(
                "u1", scope="workspace", scope_id="w1", text="必须用 pnpm", kind=KIND_HARD
            )
            assert again
        finally:
            await db.close()

    asyncio.run(go())


# ---------------------------------------------------------------- 渲染与预算


def test_render_respects_budget_and_reports_omitted() -> None:
    """超预算时按 priority 截断，并且必须显式说出省略了几条（不静默丢弃）。"""
    items = [
        Constraint(id=i, scope="session", scope_id="s1", text="必须" + "很长的约束" * 8, kind=KIND_HARD, priority=200)
        for i in range(1, 6)
    ] + [
        Constraint(id=99, scope="session", scope_id="s1", text="记住短偏好", kind=KIND_SOFT, priority=100)
    ]
    text, injected = render_constraints(items, budget_chars=200)
    assert injected < len(items)
    assert f"另有 {len(items) - injected} 条约束" in text
    assert text.startswith("【硬约束（必须遵守）】")


def test_render_empty_returns_blank() -> None:
    assert render_constraints([], budget_chars=200) == ("", 0)


# ---------------------------------------------------------------- 关键结构保证


def test_constraint_survives_compression() -> None:
    """压缩之后约束仍在 system prompt 里——因为约束不在被压缩的消息序列中。

    这是整套设计的核心断言：即使历史被摘要整体替换（原始消息彻底离开上下文），
    约束段依旧完整地在 messages[0] 里。
    """

    async def go() -> None:
        db, store, constraints = await _fresh()
        try:
            await constraints.capture_message(
                "u1",
                session_id="s1",
                workspace_id=None,
                message="必须使用 Python 3.11，禁止修改 migrations/ 目录。",
            )
            block, injected, omitted = await constraints.render_for_prompt(
                "u1", session_id="s1"
            )
            assert injected == 2 and omitted == 0

            # 造出「已压缩」的历史：有旧摘要 + 足够多的新消息触发再次压缩
            await store.save_summary("u1", "s1", 0, "早期摘要：用户想要一个排序工具。")
            for i in range(12):
                await store.add_user_message("u1", "s1", f"第{i}轮：继续推进任务 {i}")

            class _Completions:
                async def create(self, **kwargs):
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="新一代摘要"))],
                        usage=None,
                    )

            client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
            configured = ConfiguredRun(
                request=SimpleNamespace(user_id="u1", session_id="s1"),
                definition=SimpleNamespace(api_model="test-model", context_window=50),
                api_key="sk-test",
                system_instruction="BASE\n\n" + block,
                image_urls=[],
                message="当前消息",
            )
            tracer = new_tracer(None, request_id="r1", user_id="u1", session_id="s1")
            messages, _ = await summary_compress(
                configured, store, "u1", "s1", client, tracer
            )

            # 历史已被摘要替换
            assert all("第0轮" not in str(m.get("content", "")) for m in messages[1:])
            # 但约束原样留在 system 里
            system = messages[0]
            assert system["role"] == "system"
            assert "必须使用 Python 3.11" in system["content"]
            assert "禁止修改 migrations/ 目录" in system["content"]
        finally:
            await db.close()

    asyncio.run(go())
