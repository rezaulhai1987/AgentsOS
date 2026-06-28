"""Tests for the watchdog (v0.3.2).

Verifies the three public behaviors:
  - Subgoals whose claimed_at is older than the threshold are parked
    and a `subgoal.stuck` event is emitted.
  - Goals whose deadline has passed are marked failed and a
    `goal.deadline_missed` event is emitted.
  - Fresh subgoals and goals with no deadline are left alone.

We use `tick()` directly (no real sleeping) so the tests are fast and
deterministic. The watch dog's `start()` is only exercised in the
daemon smoke test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentsos.store import Store
from agentsos.watchdog import Watchdog


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "wd.db")
    yield s
    s.close()


@pytest.fixture
def watchdog(store: Store) -> Watchdog:
    return Watchdog(store, interval_s=10.0, stuck_threshold_s=300.0)


async def _set_claimed_at(s: Store, subgoal_id: str, ts: datetime) -> None:
    """Backdate the claimed_at column so the watchdog sees it as stale."""
    with sqlite3.connect(str(s.path)) as conn:
        conn.execute(
            "UPDATE subgoals SET claimed_at = ? WHERE id = ?",
            (ts.isoformat(timespec="microseconds"), subgoal_id),
        )
        conn.commit()


async def test_watchdog_marks_stuck_subgoal_as_failed(
    store: Store, watchdog: Watchdog
) -> None:
    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "m@1", "x")
    store.subgoal_claim_next(g.id)
    # Backdate to 10 min ago (> 300s threshold)
    await _set_claimed_at(store, sg.id, datetime.now(UTC) - timedelta(minutes=10))

    await watchdog.tick()

    refetched = store.subgoal_get(sg.id)
    assert refetched.status == "failed"
    assert "watchdog" in (refetched.last_error or "")


async def test_watchdog_emits_event_on_stuck(
    store: Store, watchdog: Watchdog
) -> None:
    received: list[dict] = []

    async def on_stuck(payload: dict) -> None:
        received.append(payload)

    watchdog.on("subgoal.stuck", on_stuck)

    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "m@1", "x")
    store.subgoal_claim_next(g.id)
    await _set_claimed_at(store, sg.id, datetime.now(UTC) - timedelta(minutes=10))

    await watchdog.tick()

    assert len(received) == 1
    assert received[0]["subgoal_id"] == sg.id
    assert received[0]["goal_id"] == g.id
    assert received[0]["age_s"] >= 300


async def test_watchdog_ignores_fresh_subgoals(
    store: Store, watchdog: Watchdog
) -> None:
    received: list[dict] = []

    async def on_stuck(payload: dict) -> None:
        received.append(payload)

    watchdog.on("subgoal.stuck", on_stuck)

    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "m@1", "x")
    store.subgoal_claim_next(g.id)
    # Leave claimed_at as now (fresh)

    await watchdog.tick()

    refetched = store.subgoal_get(sg.id)
    assert refetched.status == "running"  # not parked
    assert received == []


async def test_watchdog_marks_deadline_missed_goal(
    store: Store, watchdog: Watchdog
) -> None:
    received: list[dict] = []

    async def on_missed(payload: dict) -> None:
        received.append(payload)

    watchdog.on("goal.deadline_missed", on_missed)

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="microseconds")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="microseconds")
    g_past = store.goal_create("g-past", "desc", deadline=past)
    g_future = store.goal_create("g-future", "desc", deadline=future)
    g_none = store.goal_create("g-none", "desc", deadline=None)

    await watchdog.tick()

    assert store.goal_get(g_past.id).status == "failed"
    assert store.goal_get(g_past.id).finished_at is not None
    assert store.goal_get(g_future.id).status == "active"
    assert store.goal_get(g_none.id).status == "active"

    assert len(received) == 1
    assert received[0]["goal_id"] == g_past.id


async def test_watchdog_ignores_deadline_in_far_future(
    store: Store, watchdog: Watchdog
) -> None:
    far = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    g = store.goal_create("g-far", "desc", deadline=far)
    await watchdog.tick()
    assert store.goal_get(g.id).status == "active"


async def test_watchdog_start_stop(store: Store) -> None:
    wd = Watchdog(store, interval_s=0.05)
    await wd.start()
    assert wd.stats()["running"] is True
    await wd.stop()
    assert wd.stats()["running"] is False


async def test_watchdog_handler_exception_does_not_break_tick(
    store: Store, watchdog: Watchdog
) -> None:
    async def bad(_payload: dict) -> None:
        raise RuntimeError("simulated")

    async def good(payload: dict) -> None:
        good.received.append(payload)

    good.received = []  # type: ignore[attr-defined]

    watchdog.on("subgoal.stuck", bad)
    watchdog.on("subgoal.stuck", good)

    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "m@1", "x")
    store.subgoal_claim_next(g.id)
    await _set_claimed_at(store, sg.id, datetime.now(UTC) - timedelta(minutes=10))

    # Should NOT raise even though `bad` blows up
    await watchdog.tick()

    assert good.received  # type: ignore[attr-defined]
    assert len(good.received) == 1  # type: ignore[attr-defined]