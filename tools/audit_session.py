"""Baseline token audit for a saved Hermes session export.

Usage: python tools/audit_session.py <session_id>
Prints per-role token counts and a rough USD estimate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")  # close enough to Claude for budgeting


def count(text: str) -> int:
    if not text:
        return 0
    return len(ENC.encode(text))


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python tools/audit_session.py <session_id>", file=sys.stderr)
        sys.exit(2)

    sid = sys.argv[1]
    # Hermes stores sessions in the home session DB. We use the session_search
    # MCP tool to retrieve messages; for offline auditing we accept a JSON file.
    src = Path(f"audit_{sid}.json")
    if not src.exists():
        print(f"missing {src} — run `hermes session export {sid}` first", file=sys.stderr)
        sys.exit(2)

    data = json.loads(src.read_text(encoding="utf-8"))
    messages = data.get("messages") or data
    totals = {"user": 0, "assistant": 0, "tool": 0}
    per_msg = []
    for m in messages:
        role = m.get("role", "tool")
        content = m.get("content", "")
        if isinstance(content, list):  # multimodal
            text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        else:
            text = content
        n = count(text)
        totals[role] = totals.get(role, 0) + n
        per_msg.append((role, n))

    total = sum(totals.values())
    print(f"session {sid}  messages={len(per_msg)}  total_tokens={total:,}")
    for role, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        pct = (n / total * 100) if total else 0
        print(f"  {role:10s}  {n:>10,}  {pct:5.1f}%")

    # Rough pricing: Anthropic Sonnet $3/M input, $15/M output.
    # We don't separate in/out per role here, so assume 80/20 input/output split.
    est_cost = (total * 0.8 / 1e6) * 3 + (total * 0.2 / 1e6) * 15
    print(f"  est_cost_usd  ${est_cost:.4f}  (Sonnet 80/20 split, no caching)")


if __name__ == "__main__":
    main()
