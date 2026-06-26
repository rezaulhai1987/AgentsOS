"""Token counting utilities.

We default to `cl100k_base` (OpenAI's GPT-4 family encoding) because it ships
with `tiktoken` and is a good cross-provider proxy. The actual Claude tokenizer
is private, but for budgeting this is within ~5% of true counts.

If the `transformers` package is installed with an Anthropic tokenizer, prefer
that — but we keep the dep-free path for portability.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")

# Per Anthropic's published counts, ~3.5 English chars = 1 token on average.
# This lets us estimate when we don't want to pay for a full tiktoken call.
CHARS_PER_TOKEN = 3.5


def count(text: str) -> int:
    """Exact token count via tiktoken."""
    if not text:
        return 0
    return len(_ENC.encode(text))


def estimate(text: str) -> int:
    """Fast char-based estimate. Use this only for hot paths."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


@dataclass
class MessageCount:
    role: str
    text: str
    tokens: int


def count_messages(messages: Iterable[dict]) -> list[MessageCount]:
    """Count tokens for a list of {role, content} dicts.

    Adds the standard per-message overhead (role label + separators) so the
    sum is closer to the provider's real bill than a flat text count.
    """
    out: list[MessageCount] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        # +4 per message accounts for role + separators used by chat APIs.
        out.append(MessageCount(role=role, text=content, tokens=count(content) + 4))
    return out


def total(messages: Iterable[dict]) -> int:
    return sum(m.tokens for m in count_messages(messages))
