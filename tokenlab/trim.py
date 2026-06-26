"""Truncate large tool results so they don't bloat the context.

The pattern, used by Claude Code and Cursor:
  - cap each tool result to `max_bytes`
  - keep the first `head_lines` and last `tail_lines`
  - write the full result to disk under `<run_dir>/spill/<n>.txt`
  - replace the in-context result with a small "see spill/N" pointer
  - the agent can re-read spill/N later via the file tool

This is the single biggest token win for code agents: a 50kB file read
becomes a 200-token pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .count import count


@dataclass
class TruncationResult:
    text: str
    spilled_to: Path | None
    bytes_in: int
    bytes_out: int
    tokens_saved: int


def truncate_tool_result(
    text: str,
    *,
    max_bytes: int = 8_000,
    head_lines: int = 40,
    tail_lines: int = 10,
    spill_dir: Path | None = None,
    name: str = "result",
) -> TruncationResult:
    """Truncate a tool result. Returns the (possibly shortened) text.

    If the input exceeds `max_bytes`, the full content is written to
    `spill_dir/<name>.txt` and the returned text is a head+ellipsis+tail
    view that totals under `max_bytes`.
    """
    raw = text.encode("utf-8", errors="replace")
    in_size = len(raw)
    if in_size <= max_bytes:
        return TruncationResult(
            text=text,
            spilled_to=None,
            bytes_in=in_size,
            bytes_out=in_size,
            tokens_saved=0,
        )

    spill: Path | None = None
    if spill_dir is not None:
        spill_dir.mkdir(parents=True, exist_ok=True)
        spill = spill_dir / f"{name}.txt"
        spill.write_text(text, encoding="utf-8")

    lines = text.splitlines()
    head = lines[:head_lines]
    tail = lines[-tail_lines:] if tail_lines else []
    omitted = len(lines) - len(head) - len(tail)
    snippet = "\n".join(head)
    if omitted > 0:
        snippet += f"\n\n[… {omitted} lines elided. Full output at {spill} …]\n\n"
    snippet += "\n".join(tail)
    out_bytes = len(snippet.encode("utf-8"))
    tokens_in = count(text)
    tokens_out = count(snippet)
    return TruncationResult(
        text=snippet,
        spilled_to=spill,
        bytes_in=in_size,
        bytes_out=out_bytes,
        tokens_saved=tokens_in - tokens_out,
    )


def truncate_tool_messages(
    messages: list[dict],
    *,
    max_bytes: int = 8_000,
    spill_dir: Path | None = None,
) -> list[dict]:
    """Apply truncate_tool_result to every `tool` message in `messages`.

    Returns a NEW list — does not mutate in place.
    """
    out: list[dict] = []
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            out.append(m)
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            out.append(m)
            continue
        r = truncate_tool_result(
            content,
            max_bytes=max_bytes,
            spill_dir=spill_dir,
            name=f"tool_{i}",
        )
        out.append({**m, "content": r.text})
    return out
