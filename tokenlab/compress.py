"""Context compression: keep the system + recent messages, summarize the middle.

This is the same idea Claude Code, Hermes, and Cursor use to keep long
agent loops in budget. Quality is preserved by:
  - always keeping the system prompt verbatim
  - always keeping the last N user/assistant turns verbatim
  - summarizing the middle ONCE per (turn, N) and pinning that summary

We don't summarize with a model here — we use a heuristic summarizer
(line dedup + repeated-token pruning + tail-keep) so the module has zero
extra LLM calls. Callers can swap in a model summarizer via
`set_summarizer()`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .count import count

# --- heuristic summarizer --------------------------------------------------


def heuristic_summarize(messages: list[dict]) -> str:
    """Cheap, model-free summary of a message span.

    Extracts:
      - the unique lines that appear at least twice (recurring themes)
      - the most recent assistant action (likely still relevant)
      - a line count + char count for orientation

    Good enough for "context anchor" use; replace with a model call for
    higher fidelity.
    """
    if not messages:
        return ""

    counts: dict[str, int] = {}
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        for line in content.splitlines():
            line = line.strip()
            if 8 <= len(line) <= 240:
                counts[line] = counts.get(line, 0) + 1

    recurring = [line for line, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5] if n >= 2]
    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    last = ""
    if last_assistant:
        c = last_assistant.get("content", "")
        if isinstance(c, str):
            last = c.strip()[-200:]

    parts = [
        f"[summary of {len(messages)} earlier messages]",
    ]
    if recurring:
        parts.append("Recurring: " + " | ".join(recurring))
    if last:
        parts.append(f"Last assistant action: …{last}")
    return "\n".join(parts)


# --- public API ------------------------------------------------------------


Summarizer = Callable[[list[dict]], str]


@dataclass
class Compressor:
    """Sliding-window + middle-summarization compressor.

    Parameters
    ----------
    keep_last : int
        Number of recent message turns to keep verbatim.
    trigger_at : int
        When total message tokens exceed this, compress the middle.
    summarizer : callable
        A function that takes a list of messages and returns a summary string.
    """

    keep_last: int = 6
    trigger_at: int = 12_000
    summarizer: Summarizer = heuristic_summarize
    cache: dict[str, str] = field(default_factory=dict)

    def _span_key(self, messages: list[dict]) -> str:
        h = hashlib.sha256()
        for m in messages:
            h.update(m.get("role", "").encode())
            h.update(b"|")
            c = m.get("content", "")
            if not isinstance(c, str):
                c = str(c)
            h.update(c.encode())
            h.update(b"\n")
        return h.hexdigest()[:16]

    def compress(self, messages: list[dict]) -> list[dict]:
        """Compress in place; returns the new message list."""
        total_tokens = sum(count(m.get("content", "")) for m in messages)
        if total_tokens <= self.trigger_at or len(messages) <= self.keep_last + 2:
            return messages

        # Always keep the system message + the last N turns.
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        head = non_system[: max(0, len(non_system) - self.keep_last)]
        tail = non_system[-self.keep_last :] if self.keep_last else []

        if not head:
            return messages

        key = self._span_key(head)
        summary = self.cache.get(key)
        if summary is None:
            summary = self.summarizer(head)
            self.cache[key] = summary

        anchor: dict[str, Any] = {
            "role": "system",
            "content": f"[Earlier context summary]\n{summary}",
        }
        # If no real system message existed, just use the anchor.
        return [*system, anchor, *tail]

    def stats(self, original: list[dict], compressed: list[dict]) -> dict[str, int]:
        before = sum(count(m.get("content", "")) for m in original)
        after = sum(count(m.get("content", "")) for m in compressed)
        return {
            "tokens_before": before,
            "tokens_after": after,
            "tokens_saved": before - after,
            "ratio": (1 - after / before) if before else 0.0,
        }
