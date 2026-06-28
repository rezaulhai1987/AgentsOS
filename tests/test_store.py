"""Tests for the SQLite-backed Store (v0.3.1).

Covers the API the daemon + watchdog + cost guard + Telegram bridge all
rely on: goal CRUD, subgoal creation + atomic claim, cost ledger
rollups, and the healthcheck aggregate.

Concurrency: the store uses BEGIN IMMEDIATE for claim_next, so two
threads racing the same goal must not both win the same subgoal. We
exercise this with real threads + a small barrier.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentsos.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_goal_create_and_get(store: Store) -> None:
    g = store.goal_create(
        name="quarterly-report",
        description="Compile Q2 numbers into a 5-page report",
        cost_budget=2.5,
        deadline="2026-07-15T00:00:00+00:00",
    )
    assert g.id.startswith("goal-")
    assert g.name == "quarterly-report"
    assert g.status == "active"
    assert g.cost_budget == 2.5
    assert g.deadline == "2026-07-15T00:00:00+00:00"
    assert g.finished_at is None

    fetched = store.goal_get(g.id)
    assert fetched.id == g.id
    assert fetched.description == "Compile Q2 numbers into a 5-page report"


def test_goal_list_filters_by_status(store: Store) -> None:
    g1 = store.goal_create("g1", "active one")
    g2 = store.goal_create("g2", "to-pause one")
    store.goal_update_status(g2.id, "paused")
    g3 = store.goal_create("g3", "active two")

    active = store.goal_list(status="active")
    paused = store.goal_list(status="paused")

    active_ids = {g.id for g in active}
    paused_ids = {g.id for g in paused}
    assert g1.id in active_ids
    assert g3.id in active_ids
    assert g2.id not in active_ids
    assert g2.id in paused_ids


def test_goal_update_status_finished_sets_timestamp(store: Store) -> None:
    g = store.goal_create("g", "desc")
    store.goal_update_status(g.id, "done", finished=True)
    fetched = store.goal_get(g.id)
    assert fetched.status == "done"
    assert fetched.finished_at is not None


def test_goal_get_unknown_raises(store: Store) -> None:
    with pytest.raises(KeyError, match="Unknown goal"):
        store.goal_get("goal-nonexistent")


def test_subgoal_create_and_list(store: Store) -> None:
    g = store.goal_create("g", "desc")
    sg1 = store.subgoal_create(g.id, "writer@1.0.0", "draft section 1")
    sg2 = store.subgoal_create(g.id, "writer@1.0.0", "draft section 2", depends_on=[sg1.id])

    listed = store.subgoal_list(goal_id=g.id)
    assert len(listed) == 2
    assert listed[0].id == sg1.id  # ordered by created_at
    assert listed[0].depends_on == []
    assert listed[1].depends_on == [sg1.id]
    assert listed[0].status == "pending"
    assert listed[0].attempts == 0


def test_subgoal_claim_next_respects_dependencies(store: Store) -> None:
    g = store.goal_create("g", "desc")
    sg1 = store.subgoal_create(g.id, "writer@1.0.0", "first")
    sg2 = store.subgoal_create(g.id, "writer@1.0.0", "second", depends_on=[sg1.id])

    # sg1 is free, sg2 is blocked
    first_claim = store.subgoal_claim_next(g.id)
    assert first_claim is not None
    assert first_claim.id == sg1.id
    assert first_claim.status == "running"
    assert first_claim.attempts == 1

    # sg2 still blocked — sg1 is running, not done
    blocked = store.subgoal_claim_next(g.id)
    assert blocked is None

    # complete sg1, sg2 should now be claimable
    store.subgoal_complete(sg1.id, output="done", checkpoint_path="/tmp/c1.json")
    next_claim = store.subgoal_claim_next(g.id)
    assert next_claim is not None
    assert next_claim.id == sg2.id


def test_subgoal_claim_next_returns_none_when_no_pending(store: Store) -> None:
    g = store.goal_create("g", "desc")
    assert store.subgoal_claim_next(g.id) is None


def test_subgoal_claim_next_atomic_under_concurrency(store: Store) -> None:
    """Two threads racing for one pending subgoal: only one wins."""
    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "writer@1.0.0", "only one")

    winners: list[str] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def race() -> None:
        barrier.wait()
        claimed = store.subgoal_claim_next(g.id)
        if claimed is not None:
            with lock:
                winners.append(claimed.id)

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(winners) == 1
    assert winners[0] == sg.id


def test_subgoal_fail_returns_to_pending_for_retry(store: Store) -> None:
    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "writer@1.0.0", "x")
    store.subgoal_claim_next(g.id)  # running
    store.subgoal_fail(sg.id, error="network blip")
    refetched = store.subgoal_get(sg.id)
    assert refetched.status == "pending"
    assert refetched.last_error == "network blip"
    # second claim should succeed (attempts still 1 from the previous claim)
    again = store.subgoal_claim_next(g.id)
    assert again is not None
    assert again.attempts == 2


def test_subgoal_park_writes_to_dlq(store: Store) -> None:
    g = store.goal_create("g", "desc")
    sg = store.subgoal_create(g.id, "writer@1.0.0", "x")
    store.subgoal_claim_next(g.id)
    store.subgoal_park(sg.id, error="exhausted retries")

    refetched = store.subgoal_get(sg.id)
    assert refetched.status == "failed"
    assert refetched.last_error == "exhausted retries"

    dlq = store.dlq_list()
    assert len(dlq) == 1
    assert dlq[0]["subgoal_id"] == sg.id
    assert dlq[0]["reason"] == "exhausted retries"
    assert dlq[0]["replayed"] == 0


def test_cost_record_and_sum(store: Store) -> None:
    g = store.goal_create("g", "desc")
    r1 = store.cost_record(
        run_id="run-1", agent_name="writer",
        tokens_in=100, tokens_out=50, cost_usd=0.001,
        goal_id=g.id, subgoal_id="sg-x",
    )
    store.cost_record(
        run_id="run-2", agent_name="reviewer",
        tokens_in=200, tokens_out=80, cost_usd=0.002,
        goal_id=g.id, subgoal_id="sg-y",
    )
    # Unrelated cost
    store.cost_record(
        run_id="run-3", agent_name="other",
        tokens_in=999, tokens_out=999, cost_usd=9.99,
    )

    tin, tout, cost = store.cost_sum(goal_id=g.id)
    assert tin == 300
    assert tout == 130
    assert cost == pytest.approx(0.003)

    all_tin, all_tout, all_cost = store.cost_sum()
    assert all_tin == 1299
    # 0.001 + 0.002 + 9.99 = 9.993 (exact sum, but use approx for float safety)
    assert all_cost == pytest.approx(9.993)


def test_cost_sum_since_window(store: Store) -> None:
    g = store.goal_create("g", "desc")
    store.cost_record("r1", "w", 10, 5, 0.01, goal_id=g.id)
    # Mark an old record by direct UPDATE
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute(
            "UPDATE cost_ledger SET ts = ? WHERE run_id = ?",
            ("2025-01-01T00:00:00+00:00", "r1"),
        )
        conn.commit()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="microseconds")
    tin, tout, cost = store.cost_sum(goal_id=g.id, since=yesterday)
    assert tin == 0
    assert cost == 0.0


def test_healthcheck_returns_counts(store: Store) -> None:
    g1 = store.goal_create("g1", "active")
    g2 = store.goal_create("g2", "to-pause")
    store.goal_update_status(g2.id, "paused")

    sg_pending = store.subgoal_create(g1.id, "m@1", "still pending")
    sg_running = store.subgoal_create(g1.id, "m@1", "running")
    store.subgoal_claim_next(g1.id)  # sg_pending claimed -> running

    sg_failed = store.subgoal_create(g1.id, "m@1", "to-fail")
    store.subgoal_claim_next(g1.id)  # sg_running claimed -> running
    # sg_failed is the only pending; claim it then park it
    store.subgoal_claim_next(g1.id)
    store.subgoal_park(sg_failed.id, error="fail")

    store.cost_record("r1", "w", 100, 50, 0.01)

    h = store.healthcheck()
    assert h["active_goals"] == 1
    assert h["pending_subgoals"] == 0
    assert h["running_subgoals"] == 2  # sg_pending + sg_running both still running
    assert h["failed_subgoals"] == 1
    assert h["dlq_entries"] == 1
    assert h["cost_today_usd"] == pytest.approx(0.01)


def test_wal_mode_engaged(store: Store) -> None:
    with sqlite3.connect(str(store.path)) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_unknown_subgoal_raises(store: Store) -> None:
    with pytest.raises(KeyError, match="Unknown subgoal"):
        store.subgoal_get("sg-nope")
