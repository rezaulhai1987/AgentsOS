"""Tests for the LLM client abstraction.

The runtime talks to `LLMClient`, not to specific provider SDKs. These tests
pin the contract that provider adapters must satisfy: the interface,
token accounting, and the content/tool-call shape the rest of the runtime
will build on.
"""

from __future__ import annotations

import pytest

from agentsos.llm.fake import FakeClient
from agentsos.llm_client import (
    Completion,
    LLMClient,
    Message,
    ToolCall,
    ToolSpec,
    get_client,
)

# --- interface ----------------------------------------------------------------


def test_llm_client_is_abstract() -> None:
    """LLMClient must be abstract — never instantiated directly."""
    with pytest.raises(TypeError):
        LLMClient()  # type: ignore[abstract]


def test_message_round_trip_to_dict() -> None:
    """The dict form is what wire-protocol adapters will serialise."""
    m = Message(role="user", content="hello")
    assert m.to_dict() == {"role": "user", "content": "hello"}
    m2 = Message.from_dict({"role": "assistant", "content": "hi"})
    assert m2.role == "assistant" and m2.content == "hi"


def test_tool_call_round_trip() -> None:
    """Tool calls are how the agent acts — the shape must be exact."""
    tc = ToolCall(id="call_1", name="echo", arguments={"text": "hi"})
    d = tc.to_dict()
    assert d == {"id": "call_1", "name": "echo", "arguments": {"text": "hi"}}
    tc2 = ToolCall.from_dict(d)
    assert tc2 == tc


def test_tool_spec_to_provider_dict_openai_shape() -> None:
    """ToolSpec must serialise to the OpenAI function-calling shape because
    that's the de-facto wire format most providers speak."""
    spec = ToolSpec(
        name="echo",
        description="Echo a string back.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    d = spec.to_provider_dict()
    assert d == {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo a string back.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }


# --- fake client --------------------------------------------------------------


async def test_fake_client_returns_recorded_response() -> None:
    """The FakeClient is what runtime tests use — record a response, replay it."""
    fake = FakeClient()
    fake.record("hello", Completion(message=Message("assistant", "hi"), tokens_in=2, tokens_out=1))
    c = await fake.complete([Message("user", "hello")], model="x", tools=())
    assert c.message.content == "hi"
    assert c.tokens_in == 2 and c.tokens_out == 1


async def test_fake_client_records_every_call() -> None:
    """Runtime tests assert call shape — FakeClient must keep history."""
    fake = FakeClient()
    fake.record_default(Completion(message=Message("assistant", "ok"), tokens_in=1, tokens_out=1))
    await fake.complete([Message("user", "first")], model="x", tools=())
    await fake.complete([Message("user", "second")], model="x", tools=())
    assert [c.messages[0].content for c in fake.calls] == ["first", "second"]


async def test_fake_client_errors_when_unrecorded() -> None:
    """Unrecorded prompts should fail loud, not silently echo the goal."""
    fake = FakeClient()
    with pytest.raises(KeyError):
        await fake.complete([Message("user", "never recorded")], model="x", tools=())


async def test_fake_client_accounts_tokens_via_tokenlab() -> None:
    """If a test doesn't pin tokens explicitly, the fake should compute them
    via tokenlab so budget assertions are still meaningful."""
    fake = FakeClient()  # no recorded tokens
    fake.record(
        "hi",
        Completion(message=Message("assistant", "hello there"), tokens_in=0, tokens_out=0),
    )
    c = await fake.complete([Message("user", "hi")], model="x", tools=())
    assert c.tokens_in > 0
    assert c.tokens_out > 0


# --- registry -----------------------------------------------------------------


def test_get_client_returns_fake_when_provider_is_fake() -> None:
    """The registry must dispatch by provider name. `fake` is the test entry point."""
    c = get_client("fake")
    assert isinstance(c, FakeClient)


def test_get_client_dispatches_by_provider() -> None:
    """Provider name -> adapter class. Unknown providers raise so the user
    gets a clear error instead of a silent default."""
    from agentsos.llm.openai_compat import OpenAICompatClient

    assert isinstance(get_client("openai"), OpenAICompatClient)
    assert isinstance(get_client("anthropic"), OpenAICompatClient)
    with pytest.raises(ValueError, match="Unknown provider"):
        get_client("does-not-exist")
