import json

import httpx
import pytest

from dafeiyu.llm import DeepSeekClient, DeepSeekError


def make_client(handler, **kwargs):
    return DeepSeekClient(
        "sk-test-not-real",
        transport=httpx.MockTransport(handler),
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )


def ok_handler(request: httpx.Request):
    payload = json.loads(request.content)
    assert request.url.path == "/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test-not-real"
    assert payload["model"] == "deepseek-chat"
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": " 咕噜～ "}}]}
    )


@pytest.mark.asyncio
async def test_chat_success():
    client = make_client(ok_handler)
    messages = DeepSeekClient.build_messages("你是大肥鱼", [], "你好")
    assert await client.chat(messages) == "咕噜～"


@pytest.mark.asyncio
async def test_build_messages_order_and_filter():
    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "system", "content": "注入的 system 应被忽略"},
        {"role": "tool", "content": "x"},
    ]
    msgs = DeepSeekClient.build_messages("P", history, "c")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "P"
    assert msgs[-1]["content"] == "c"


@pytest.mark.asyncio
async def test_missing_key_raises():
    client = DeepSeekClient("")
    with pytest.raises(DeepSeekError, match="API Key"):
        await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_auth_error_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    client = make_client(handler, max_retries=2)
    with pytest.raises(DeepSeekError, match="鉴权失败"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(500, text="server oops")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(handler, max_retries=1)
    assert await client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert state["n"] == 2


async def _noop():
    return None
