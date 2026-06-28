"""In-memory LLM client for tests.

Three ways to script responses:

1. ``record(prompt_substring, completion)`` — match by substring of the
   last user message. Useful for single-turn tests.
2. ``record_default(completion)`` — fallback when nothing else matches.
3. ``script([c1, c2, ...])`` — an ordered queue; each ``complete()``
   consumes the next entry. Useful for multi-turn loop tests where the
   conversation grows and prompt-key matching becomes ambiguous.

If the recorded ``Completion`` has ``tokens_in``/``tokens_out`` left at 0,
the fake derives them via ``tokenlab.count`` so the runtime's cost
accounting assertions stay truthful.
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
        self._script: list[Completion] = []
        self.calls: list[_Call] = []

    def record(self, key: str, completion: Completion) -> None:
        """Map a prompt-substring key to a canned response (substring match)."""
        self._responses[key] = completion

    def record_default(self, completion: Completion) -> None:
        """Catch-all response when no substring key matches."""
        self._responses["__default__"] = completion

    def script(self, completions: Sequence[Completion]) -> None:
        """Set an ordered list of responses. Each `complete()` pops the next
        one. Lets tests drive multi-turn loops without worrying about
        substring collisions."""
        self._script = list(completions)

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

        # Scripted responses take precedence — they make multi-turn tests
        # deterministic regardless of how the transcript grows.
        if self._script:
            recorded = self._script.pop(0)
        else:
            user_messages = [m for m in messages if m.role == "user"]
            if not user_messages:
                raise KeyError("FakeClient requires at least one user message")
            last_user = user_messages[-1].content
            recorded = None
            for needle, response in self._responses.items():
                if needle == "__default__":
                    continue
                if needle in last_user:
                    recorded = response
                    break
            if recorded is None:
                recorded = self._responses.get("__default__")
            if recorded is None:
                raise KeyError(f"FakeClient has no recorded response for prompt {last_user!r}")

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
            tool_calls=recorded.tool_calls,
        )
