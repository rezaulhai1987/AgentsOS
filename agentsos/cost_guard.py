"""Cost guard — enforces per-day and per-goal USD ceilings.

The runtime calls `await guard.check(goal_id, pending_cost_usd=0)`
before each step. The guard checks:
  1. The goal's `cost_budget` (per-goal ceiling).
  2. The daily `daily_ceiling` (configurable globally; optional per-goal
     override).

If either would be breached by `pending_cost_usd` more of spending,
`check` returns False (the caller should halt the run).

The guard is intentionally non-raising: returns a bool, doesn't abort
the loop. The reactor / orchestrator decides what to do — halt the
goal, park the subgoal, alert the operator. This keeps the guard
single-purpose.

A small in-memory callback bus lets v0.5 alerts subscribe to
`cost.ceiling_breached` events.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agentsos.store import Store

log = logging.getLogger("agentsos.cost_guard")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CostCeilingBreached(Exception):
    def __init__(self, reason: str, payload: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload = payload


class CostGuard:
    def __init__(
        self,
        store: Store,
        daily_ceiling_usd: float = 50.0,
    ) -> None:
        self.store = store
        self.daily_ceiling_usd = daily_ceiling_usd
        self._handlers: dict[str, list[EventHandler]] = {}

    def on(self, topic: str, handler: EventHandler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    async def emit(self, topic: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers.get(topic, [])):
            try:
                await handler(payload)
            except Exception as exc:  # pragma: no cover
                log.warning("cost-guard handler for %s raised: %s", topic, exc)

    def _day_start_iso(self) -> str:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(timespec="microseconds")

    async def check(
        self,
        goal_id: str,
        pending_cost_usd: float = 0.0,
    ) -> bool:
        """Return True if `pending_cost_usd` more of spending is allowed.

        False means: at least one ceiling would be breached.
        """
        try:
            goal = self.store.goal_get(goal_id)
        except KeyError:
            # Unknown goal — treat as no cap (caller will discover the
            # bug elsewhere). Better than silently blocking.
            return True

        # Per-goal spend
        _, _, goal_spent = self.store.cost_sum(goal_id=goal_id)
        goal_remaining = goal.cost_budget - goal_spent
        if goal_remaining < pending_cost_usd:
            payload = {
                "scope": "goal",
                "goal_id": goal_id,
                "goal_name": goal.name,
                "budget": goal.cost_budget,
                "spent": round(goal_spent, 6),
                "pending": round(pending_cost_usd, 6),
                "remaining": round(goal_remaining, 6),
                "ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            }
            await self.emit("cost.ceiling_breached", payload)
            log.warning("goal %s breached budget (%.4f/%.4f)", goal_id, goal_spent, goal.cost_budget)
            return False

        # Daily ceiling
        day_ceiling = goal.daily_ceiling if goal.daily_ceiling is not None else self.daily_ceiling_usd
        _, _, day_spent = self.store.cost_sum(since=self._day_start_iso())
        day_remaining = day_ceiling - day_spent
        if day_remaining < pending_cost_usd:
            payload = {
                "scope": "daily",
                "goal_id": goal_id,
                "ceiling": day_ceiling,
                "spent": round(day_spent, 6),
                "pending": round(pending_cost_usd, 6),
                "remaining": round(day_remaining, 6),
                "ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            }
            await self.emit("cost.ceiling_breached", payload)
            log.warning(
                "daily ceiling breached (goal=%s, spent=%.4f, ceiling=%.4f)",
                goal_id,
                day_spent,
                day_ceiling,
            )
            return False

        return True

    def snapshot(self, goal_id: str | None = None) -> dict[str, Any]:
        today = self._day_start_iso()
        _, _, today_cost = self.store.cost_sum(since=today)
        goal_block: dict[str, Any] | None = None
        if goal_id is not None:
            try:
                goal = self.store.goal_get(goal_id)
                _, _, spent = self.store.cost_sum(goal_id=goal_id)
                goal_block = {
                    "id": goal.id,
                    "name": goal.name,
                    "budget": goal.cost_budget,
                    "spent": round(spent, 6),
                    "remaining": round(goal.cost_budget - spent, 6),
                }
            except KeyError:
                pass
        return {
            "daily_ceiling_usd": self.daily_ceiling_usd,
            "cost_today_usd": round(today_cost, 6),
            "goal": goal_block,
        }