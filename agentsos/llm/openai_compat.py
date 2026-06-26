"""OpenAI-compatible chat-completions adapter.

Covers OpenAI itself, OpenRouter, llama.cpp's server, vLLM, Groq, Together,
and any other endpoint that speaks `/v1/chat/completions` with function
calling. `anthropic` is mapped to the same adapter because Anthropic now
offers an OpenAI-compat shim at the same path; native Anthropic SDK
support arrives when somebody needs it.

Why httpx instead of the openai SDK:
- No transitive weight; we already pull httpx for other reasons.
- Same async surface (`httpx.AsyncClient`) so the runtime doesn't fork on
  sync vs async at the I/O boundary.
- Easy to mock with `httpx.MockTransport` in tests.

Token accounting: prefer `usage.prompt_tokens` / `usage.completion_tokens`
from the provider; fall back to `tokenlab.count` for adapters that don't
report usage (llama.cpp server, some local stacks).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import httpx

from ..llm_client import (
    Completion,
    LLMClient,
    LLMError,
    Message,
    ToolCall,
    ToolSpec,
    register_client,
)


def _default_base_url(provider: str) -> str:
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1"  # via OpenAI-compat shim
    return os.environ.get(f"AGENTSOS_{provider.upper()}_BASE_URL", "http://localhost:8000/v1")


def _default_api_key(provider: str) -> str:
    env = f"{provider.upper()}_API_KEY"
    key = os.environ.get(env) or os.environ.get("OPENAI_API_KEY", "")
    return key


@register_client("openai")
@register_client("anthropic")
@register_client("llama.cpp")
@register_client("hf")
class OpenAICompatClient(LLMClient):
    """Calls /chat/completions against any OpenAI-compat endpoint."""

    def __init__(
        self,
        provider: str = "openai",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url or _default_base_url(provider)
        self.api_key = api_key if api_key is not None else _default_api_key(provider)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [t.to_provider_dict() for t in tools]

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = await self._http.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as e:
            raise LLMError(f"{self.provider}: transport error: {e}") from e

        if resp.status_code >= 400:
            # Surface provider error message verbatim — operators need it for debugging.
            raise LLMError(f"{self.provider}: HTTP {resp.status_code} — {resp.text}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}

        # Tool calls: provider may return empty list or omit entirely.
        raw_tcs = msg.get("tool_calls") or []
        tool_calls = tuple(
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=(tc.get("function") or {}).get("name") or "",
                arguments=_safe_json_loads((tc.get("function") or {}).get("arguments")),
            )
            for i, tc in enumerate(raw_tcs)
        )

        completion_msg = Message(
            role="assistant",
            content=msg.get("content") or "",
            tool_calls=tool_calls,
        )

        # Prefer provider-reported usage; fall back to tokenlab for adapters
        # that don't fill in `usage` (llama.cpp server, etc.).
        usage = data.get("usage") or {}
        ti = int(usage.get("prompt_tokens") or 0)
        to = int(usage.get("completion_tokens") or 0)
        if ti == 0 or to == 0:
            from tokenlab.count import count as count_text
            from tokenlab.count import count_messages

            if ti == 0:
                ti = sum(mc.tokens for mc in count_messages(m.to_dict() for m in messages))
            if to == 0:
                to = count_text(completion_msg.content)

        return Completion(
            message=completion_msg,
            tokens_in=ti,
            tokens_out=to,
            finish_reason=choice.get("finish_reason") or "stop",
            model=model,
            raw=data,
        )


def _safe_json_loads(s: str | None) -> dict[str, Any]:
    """Tool call arguments come back as a JSON string. If parsing fails we
    return an empty dict — the runtime will see the empty dispatch and
    surface it as a tool error rather than crashing the whole agent."""
    if not s:
        return {}
    import json

    try:
        out = json.loads(s)
    except (ValueError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}
