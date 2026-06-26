"""tokenlab — the AgentsOS token optimization toolkit.

This package implements the techniques catalogued in
`docs/TOKEN_REDUCTION_RESEARCH.md`:

  - count   : exact tiktoken-based token counting, per-message breakdown
  - budget  : per-session and per-call token budgets
  - compress: sliding-window + summarization context compression
  - cache   : exact-match + semantic response cache (file-backed)
  - router  : small-model triage -> large-model synthesis (cascade)
  - trim    : tool result truncation + disk spill
  - schema  : tool-schema minimizer (drops redundant fields)

The aim is a *real* reduction in tokens used per agent run, with measurable
budgets and a CLI for inspection. Not magic.
"""

from __future__ import annotations

__version__ = "0.1.0"
