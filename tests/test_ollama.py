"""Ollama adapter tests.

Ollama follows the OpenAI-compat wire format, so most behaviour is
already covered by `test_openai_compat.py`. These tests pin down the
things that are Ollama-specific:

- The adapter is registered under `provider: ollama`.
- Default base URL points at the local Ollama server.
- No API key is required (Authorization header is omitted).
- A user-supplied `base_url` in the manifest overrides the default
  (e.g. when Ollama runs on a remote host or custom port).
"""

from __future__ import annotations

import httpx
import pytest

from agentsos.llm.ollama import OllamaClient
from agentsos.llm_client import Message, get_client


def test_ollama_is_registered() -> None:
    """Manifest `provider: ollama` must resolve to the OllamaClient class."""
    client = get_client("ollama")
    assert isinstance(client, OllamaClient)


def test_ollama_default_base_url() -> None:
    """Out of the box, OllamaClient must point at the local Ollama server."""
    client = OllamaClient()
    assert client.base_url == "http://localhost:11434/v1"


def test_ollama_default_api_key_is_empty() -> None:
    """Ollama's local server doesn't require auth."""
    client = OllamaClient()
    assert client.api_key == ""


def test_ollama_manifest_base_url_override() -> None:
    """User can point Ollama at a remote host via the manifest."""
    client = OllamaClient(base_url="http://gpu-box.lan:11434/v1")
    assert client.base_url == "http://gpu-box.lan:11434/v1"


def test_ollama_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTSOS_OLLAMA_BASE_URL wins over the hardcoded default."""
    monkeypatch.setenv("AGENTSOS_OLLAMA_BASE_URL", "http://10.0.0.5:11434/v1")
    client = OllamaClient()
    assert client.base_url == "http://10.0.0.5:11434/v1"


@pytest.mark.asyncio
async def test_ollama_call_uses_local_endpoint_and_skips_auth() -> None:
    """A real call to Ollama goes to /chat/completions WITHOUT an
    Authorization header — that's the contract local Ollama expects."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-ollama-1",
                "model": "llama3.1:8b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello from ollama"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
        )

    transport = httpx.MockTransport(handler)
    client = OllamaClient(http=httpx.AsyncClient(transport=transport))
    try:
        completion = await client.complete(
            [Message("user", "ping")],
            model="llama3.1:8b",
        )
    finally:
        await client.aclose()

    assert "localhost:11434" in captured["url"]
    assert captured["url"].endswith("/chat/completions")
    assert "authorization" not in {k.lower() for k in captured["headers"]}
    assert completion.message.content == "hello from ollama"
    assert completion.tokens_in == 7
    assert completion.tokens_out == 4
