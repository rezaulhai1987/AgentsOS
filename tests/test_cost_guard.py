"""Tests for the cost guard (v0.3.3).

Three things to verify:
  - check() returns True under both ceilings.
  - check() returns False and emits `cost.ceiling_breached` when the
    per-goal budget would be exceeded.
  - check() returns False and emits when the daily ceiling would be
    exceeded (even if per-goal budget remains).
  - per-goal daily_ceiling override is honoured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentsos.cost_guard import CostGuard
from agentsos.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "cg.db")
    yield s
    s.close()


@pytest.fixture
def guard(store: Store) -> CostGuard:
    return CostGuard(store, daily_ceiling_usd=10.0)


async def test_cost_guard_allows_under_ceiling(store: Store, guard: CostGuard) -> None:
    g = store.goal_create("g", "desc", cost_budget=1.0)
    store.cost_record("r1", "w", 100, 50, 0.10, goal_id=g.id)
    allowed = await guard.check(g.id, pending_cost_usd=0.50)
    assert allowed is True


async def test_cost_guard_denies_over_goal_budget(store: Store, guard: CostGuard) -> None:
    received: list[dict] = []

    async def on_breach(payload: dict) -> None:
        received.append(payload)

    guard.on("cost.ceiling_breached", on_breach)

    g = store.goal_create("g", "desc", cost_budget=1.0)
    store.cost_record("r1", "w", 100, 50, 0.90, goal_id=g.id)
    allowed = await guard.check(g.id, pending_cost_usd=0.20)
    assert allowed is False
    assert len(received) == 1
    assert received[0]["scope"] == "goal"
    assert received[0]["goal_id"] == g.id


async def test_cost_guard_denies_over_daily_ceiling(
    store: Store, guard: CostGuard
) -> None:
    received: list[dict] = []

    async def on_breach(payload: dict) -> None:
        received.append(payload)

    guard.on("cost.ceiling_breached", on_breach)

    g1 = store.goal_create("g1", "desc", cost_budget=100.0)
    g2 = store.goal_create("g2", "desc", cost_budget=100.0)
    store.cost_record("r1", "w", 100, 50, 9.0, goal_id=g1.id)  # g1: 9.0
    store.cost_record("r2", "w", 100, 50, 0.50, goal_id=g2.id)  # g2: 0.50; total today: 9.5

    # Asking for 1.0 more would push today to 10.5 > 10.0 ceiling
    allowed = await guard.check(g2.id, pending_cost_usd=1.0)
    assert allowed is False
    assert received[-1]["scope"] == "daily"


async def test_per_goal_daily_ceiling_override(tmp_path: Path) -> None:
    # Global ceiling 100, but this goal has its own daily ceiling of 0.50
    local_store = Store(tmp_path / "cg2.db")
    local_guard = CostGuard(local_store, daily_ceiling_usd=100.0)
    g = local_store.goal_create("g", "desc", cost_budget=10.0, daily_ceiling=0.50)
    local_store.cost_record("r1", "w", 100, 50, 0.40, goal_id=g.id)
    allowed = await local_guard.check(g.id, pending_cost_usd=0.20)
    assert allowed is False  # 0.40 + 0.20 = 0.60 > 0.50 ceiling


async def test_snapshot_reports_totals(store: Store, guard: CostGuard) -> None:
    g = store.goal_create("g", "desc", cost_budget=2.0)
    store.cost_record("r1", "w", 100, 50, 0.30, goal_id=g.id)
    snap = guard.snapshot(goal_id=g.id)
    assert snap["daily_ceiling_usd"] == 10.0
    assert snap["cost_today_usd"] == pytest.approx(0.30)
    assert snap["goal"] is not None
    assert snap["goal"]["budget"] == 2.0
    assert snap["goal"]["spent"] == pytest.approx(0.30)
    assert snap["goal"]["remaining"] == pytest.approx(1.70)


async def test_snapshot_with_unknown_goal_returns_none(store: Store, guard: CostGuard) -> None:
    snap = guard.snapshot(goal_id="goal-does-not-exist")
    assert snap["goal"] is None