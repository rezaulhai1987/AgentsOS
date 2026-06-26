"""Token budgets.

A Budget tracks tokens used per call and per session and can refuse new
work that would exceed a cap. This is the "circuit breaker" that prevents
a runaway agent loop from spending the whole bank in one go.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .count import total


@dataclass
class BudgetExceeded(Exception):
    used: int
    limit: int
    scope: str

    def __str__(self) -> str:
        return f"token budget exceeded in {self.scope}: {self.used}/{self.limit}"


@dataclass
class Budget:
    """Token budget with separate per-call and per-session limits."""

    per_call: int = 8_000
    per_session: int = 200_000
    per_session_cost_usd: float = 5.0
    used_session: int = 0
    spent_usd: float = 0.0
    started_at: float = field(default_factory=time.time)

    def charge(self, messages: list[dict], cost_usd: float = 0.0) -> int:
        n = total(messages)
        if n > self.per_call:
            raise BudgetExceeded(n, self.per_call, "per_call")
        if self.used_session + n > self.per_session:
            raise BudgetExceeded(self.used_session + n, self.per_session, "per_session")
        if self.spent_usd + cost_usd > self.per_session_cost_usd:
            raise BudgetExceeded(
                int(self.spent_usd + cost_usd),
                int(self.per_session_cost_usd),
                "per_session_cost",
            )
        self.used_session += n
        self.spent_usd += cost_usd
        return n

    def remaining(self) -> int:
        return max(0, self.per_session - self.used_session)
