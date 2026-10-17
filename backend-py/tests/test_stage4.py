"""阶段 4 验收测试：conversation 会话列表 + Timeline 投影 + usage 用量记录。

用 mock 的 AsyncOpenAI 跑一次流式对话，验证会话持久化与历史回放。
client fixture 由 conftest.py 提供（session 级）。
"""

from __future__ import annotations


class _FakeDelta:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.reasoning_content = None
        self.model_extra = {}


class _FakeChoice:
    def __init__(self, delta: _FakeDelta) -> None:
        self.delta = delta


class _FakeUsage:
    total_tokens = 10
    completion_tokens = 6
    prompt_tokens = 4
    prompt_tokens_details = None


class _FakeChunk:
    def __init__(self, delta: _FakeDelta, usage: _FakeUsage | None = None) -> None:
        self.choices = [_FakeChoice(delta)]
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _FakeCompletions:
    async def create(self, **kwargs):
        return _FakeStream(
            [
                _FakeChunk(_FakeDelta("回复内容")),
                _FakeChunk(_FakeDelta(), usage=_FakeUsage()),
            ]
        )


class _FakeChat:
    completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, **kwargs) -> None:
        self.chat = _FakeChat()


def test_conversation_list_and_get(client, monkeypatch) -> None:
    client.put(
        "/internal/user-settings",
        json={
            "userId": "u-conv",
            "defaultModelId": "minimax.m3",
            "providers": [{"providerId": "minimax", "apiKey": "sk-test"}],
        },
    )
    monkeypatch.setattr(
        "edith_backend.internal.agentrun.execution.AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    # 跑一次流式对话
    resp = client.post(
        "/internal/gateway/messages:stream",
        json={
            "requestId": "conv-req-1",
            "userId": "u-conv",
            "sessionId": "conv-session-1",
            "message": "给我总结一下",
            "modelId": "minimax.m3",
        },
    )
    assert resp.status_code == 200

    # 会话列表包含该会话，标题为第一条用户消息
    listing = client.get("/internal/conversations?userId=u-conv").json()
    ids = {c["id"] for c in listing["conversations"]}
    assert "conv-session-1" in ids

    # 会话详情：Timeline 有 user + assistant 块，usage 有累计用量
    detail = client.get(
        "/internal/conversations/conv-session-1?userId=u-conv"
    ).json()
    blocks = detail["timeline"]["blocks"]
    assert blocks[0]["type"] == "user"
    assert blocks[0]["content"] == "给我总结一下"
    assert blocks[1]["type"] == "assistant"
    assert blocks[1]["blocks"][0]["type"] == "text"
    assert blocks[1]["blocks"][0]["content"] == "回复内容"
    assert detail["usage"]["totalTokens"] == 10
    assert detail["usage"]["completionTokens"] == 6


def test_conversation_not_found_returns_404(client) -> None:
    response = client.get("/internal/conversations/no-such?userId=u-conv")
    assert response.status_code == 404
    assert response.json()["type"] == "conversation_not_found"


def test_list_requires_user_id(client) -> None:
    response = client.get("/internal/conversations")
    assert response.status_code == 400
