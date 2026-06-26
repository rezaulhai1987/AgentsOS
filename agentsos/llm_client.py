"""LLM client abstraction.

The runtime talks to `LLMClient.complete(...)`; provider-specific HTTP and
SDK calls live in adapters under `agentsos.llm.*`. Adapters are dispatched
by `manifest.model.provider` through `get_client()`.

Design choices:

- `complete()` is async — every I/O path in the runtime already is.
- `tokens_in` / `tokens_out` on the returned `Completion` are authoritative.
  Adapters either read provider-reported usage (preferred) or fall back to
  `tokenlab.count` for local models.
- The wire format for tool calls follows OpenAI's function-calling shape
  because that's the de-facto standard most providers speak. Anthropic,
  llama.cpp, and vLLM all expose OpenAI-compat endpoints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Token accounting happens inside adapters — this module only defines the
# shapes the runtime sees. Adapters prefer provider-reported usage; when
# that's missing they fall back to `tokenlab.count` per-message.

# Public re-exports so callers can `from agentsos.llm import Message`.
__all__ = [
    "Completion",
    "LLMClient",
    "Message",
    "ToolCall",
    "ToolSpec",
    "get_client",
]


@dataclass(frozen=True)
class Message:
    """One turn in a chat transcript. `tool_call_id` is set on tool-result
    messages so the model can correlate them with the originating call."""

    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        tcs = tuple(ToolCall.from_dict(tc) for tc in d.get("tool_calls") or [])
        return cls(
            role=d["role"],
            content=d.get("content", "") or "",
            name=d.get("name"),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=tcs,
        )


@dataclass(frozen=True)
class ToolCall:
    """A model-directed request to invoke a tool. `arguments` is already
    validated JSON the runtime will dispatch against the tool registry."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolCall:
        return cls(id=d["id"], name=d["name"], arguments=dict(d.get("arguments") or {}))


@dataclass(frozen=True)
class ToolSpec:
    """A tool advertisement. The runtime registers these; the model sees the
    provider-shaped `to_provider_dict()` form."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_provider_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class Completion:
    """What an LLM call returns. `tokens_in` / `tokens_out` are the source
    of truth for cost accounting — never use len(text.split()) here."""

    message: Message
    tokens_in: int
    tokens_out: int
    finish_reason: str = "stop"
    # Optional provider-reported metadata, kept for telemetry / cost dashboards.
    model: str = ""
    # If the model wants to call tools, they appear here. Empty tuple means
    # the model returned a final answer and the loop should terminate.
    tool_calls: tuple[ToolCall, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    """Thin abstraction every provider adapter implements."""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Completion:
        """Run one chat completion. Implementations must:
        - serialise messages + tools into the provider's wire format,
        - return a `Completion` with truthful `tokens_in` / `tokens_out`,
        - raise `LLMError` on any provider-level failure.
        """


class LLMError(RuntimeError):
    """Provider-level failure. Callers can decide whether to retry or abort."""


# Provider dispatch — done by name so the runtime can construct clients
# without knowing adapter classes. Adding a new provider means: write an
# adapter class, add it to `_REGISTRY`.
_REGISTRY: dict[str, type[LLMClient]] = {}


def register_client(provider: str) -> Any:
    """Decorator: `@register_client("openai")` on an LLMClient subclass."""

    def _wrap(cls: type[LLMClient]) -> type[LLMClient]:
        if not issubclass(cls, LLMClient):
            raise TypeError(f"{cls!r} is not an LLMClient subclass")
        _REGISTRY[provider] = cls
        return cls

    return _wrap


def get_client(provider: str) -> LLMClient:
    """Look up the adapter for `provider`. `fake` is always available and
    is what the test suite uses."""
    # Lazy-import adapters so missing optional deps don't break import time.
    if not _REGISTRY:
        _load_default_adapters()
    try:
        return _REGISTRY[provider]()
    except KeyError as e:
        raise ValueError(f"Unknown provider {provider!r}. Available: {sorted(_REGISTRY)}") from e


def _load_default_adapters() -> None:
    """Import adapter modules so their `@register_client` decorators run."""
    from . import fake as _fake  # noqa: F401 — import for side effect
    from . import openai_compat  # noqa: F401
