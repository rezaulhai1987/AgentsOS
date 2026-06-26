"""In-memory LLM client for tests.

Record a response keyed by the user-prompt content, then `complete()` will
replay it. If `tokens_in` / `tokens_out` are left at 0 in the recorded
`Completion`, the fake computes them via `tokenlab.count` so the runtime's
budget assertions stay meaningful.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tokenlab.count import count as count_text
from tokenlab.count import count_messages

from ..llm_client import Completion, LLMClient, Message, ToolSpec, register_client


@dataclass
class _Call:
    """One captured `complete()` invocation. Used by tests to assert shape."""

    messages: list[Message]
    model: str
    tool_names: list[str]


@register_client("fake")
class FakeClient(LLMClient):
    def __init__(self) -> None:
        self._responses: dict[str, Completion] = {}
        self.calls: list[_Call] = []

    def record(self, key: str, completion: Completion) -> None:
        """Map a prompt-content key to a canned response."""
        self._responses[key] = completion

    def record_default(self, completion: Completion) -> None:
        """Catch-all response when the prompt key has no exact match.
        Useful when a test only cares about *that* complete() was called,
        not which prompt was used."""
        self._responses["__default__"] = completion

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Completion:
        self.calls.append(
            _Call(
                messages=list(messages),
                model=model,
                tool_names=[t.name for t in tools],
            )
        )
        # Find the last user message — that's the key the test recorded.
        user_messages = [m for m in messages if m.role == "user"]
        if not user_messages:
            raise KeyError("FakeClient requires at least one user message")
        key = user_messages[-1].content
        recorded = self._responses.get(key) or self._responses.get("__default__")
        if recorded is None:
            raise KeyError(f"FakeClient has no recorded response for prompt {key!r}")

        # If the test didn't pin tokens, derive them from tokenlab so cost
        # accounting assertions remain truthful.
        if recorded.tokens_in == 0:
            ti = sum(mc.tokens for mc in count_messages(m.to_dict() for m in messages))
        else:
            ti = recorded.tokens_in
        to = recorded.tokens_out or count_text(recorded.message.content)
        return Completion(
            message=recorded.message,
            tokens_in=ti,
            tokens_out=to,
            finish_reason=recorded.finish_reason,
            model=model,
            raw=recorded.raw,
        )
