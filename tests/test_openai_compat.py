"""OpenAI-compat adapter tests using httpx.MockTransport.

No real network — the mock transport intercepts the request, returns a
fixture response, and lets us assert the exact wire shape the adapter
sends to providers. This is what catches "we forgot to forward tools"
or "we double-serialise messages" regressions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentsos.llm.openai_compat import OpenAICompatClient
from agentsos.llm_client import (
    Completion,
    LLMError,
    Message,
    ToolSpec,
)


@pytest.fixture
def recording_transport():
    """Capture the (request, response) pair and return a controllable mock."""
    captured: dict[str, Any] = {"request": None, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["calls"] += 1
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "hello back",
                            "tool_calls": None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            },
        )

    return captured, httpx.MockTransport(handler)


async def test_openai_compat_posts_chat_completions(recording_transport) -> None:
    captured, transport = recording_transport
    http = httpx.AsyncClient(transport=transport)
    client = OpenAICompatClient(provider="openai", api_key="sk-test", http=http)
    try:
        c = await client.complete(
            [Message("user", "hi")],
            model="gpt-test",
            tools=(),
            temperature=0.5,
            max_tokens=64,
        )
    finally:
        await client.aclose()

    req = captured["request"]
    assert req.method == "POST"
    assert req.url.path.endswith("/chat/completions")
    body = json.loads(req.content)
    assert body["model"] == "gpt-test"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 64
    assert req.headers["authorization"] == "Bearer sk-test"
    assert captured["calls"] == 1

    assert c.message.content == "hello back"
    assert c.tokens_in == 11
    assert c.tokens_out == 3
    assert c.finish_reason == "stop"


async def test_openai_compat_forwards_tools(recording_transport) -> None:
    captured, transport = recording_transport
    http = httpx.AsyncClient(transport=transport)
    client = OpenAICompatClient(provider="openai", api_key="sk-test", http=http)
    try:
        await client.complete(
            [Message("user", "what is the weather?")],
            model="gpt-test",
            tools=[
                ToolSpec(
                    name="get_weather",
                    description="Get the current weather for a city.",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                )
            ],
        )
    finally:
        await client.aclose()

    body = json.loads(captured["request"].content)
    assert "tools" in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "get_weather"


async def test_openai_compat_parses_tool_calls() -> None:
    """Provider returns a tool_call → adapter parses it into a ToolCall."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Paris"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatClient(provider="openai", api_key="sk", http=http)
    try:
        c = await client.complete([Message("user", "weather?")], model="gpt-test")
    finally:
        await client.aclose()

    assert len(c.message.tool_calls) == 1
    tc = c.message.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "get_weather"
    assert tc.arguments == {"city": "Paris"}


async def test_openai_compat_raises_llm_error_on_http_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatClient(provider="openai", api_key="sk", http=http)
    try:
        with pytest.raises(LLMError, match="HTTP 500"):
            await client.complete([Message("user", "x")], model="gpt-test")
    finally:
        await client.aclose()


async def test_openai_compat_falls_back_to_tokenlab_when_usage_missing() -> None:
    """Local servers (llama.cpp) often omit `usage`. Adapter must still
    produce truthful token counts via tokenlab."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                # No usage field.
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatClient(provider="llama.cpp", api_key="", http=http)
    try:
        c = await client.complete([Message("user", "hi")], model="local-model")
    finally:
        await client.aclose()

    assert c.tokens_in > 0, "tokens_in must be derived when provider omits usage"
    assert c.tokens_out > 0, "tokens_out must be derived when provider omits usage"


async def test_openai_compat_safe_json_loads_handles_malformed_arguments() -> None:
    """A model sometimes returns non-JSON `arguments`. The adapter must NOT
    crash the whole agent — it should hand back an empty dict and let the
    runtime surface the tool error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": "not-json"},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAICompatClient(provider="openai", api_key="sk", http=http)
    try:
        c = await client.complete([Message("user", "x")], model="m")
    finally:
        await client.aclose()

    assert c.message.tool_calls[0].arguments == {}


# Silence unused-import lint if pytest fixtures are reordered later.
_ = Completion
