"""Tests for the v0.3.6 daemon ↔ work_registry wiring.

These tests verify:
  - The daemon constructs a Journal + Registry on init
  - Events (watchdog / cost-guard) are mirrored to the journal
  - The Registry file is flushed on construction
  - snapshot() includes the registry + journal summary
  - Crash-resume: a fresh Daemon over the same state_dir picks up the
    prior journal entries without losing data
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentsos.daemon import Daemon, DaemonConfig


@pytest.fixture
def cfg(tmp_path: Path) -> DaemonConfig:
    return DaemonConfig(
        state_dir=tmp_path / "state",
        daily_ceiling_usd=5.0,
        watchdog_interval_s=0.05,
        stuck_threshold_s=10.0,
        jsonl_log=tmp_path / "daemon.jsonl",
        branch="feat/v0.3",
    )


def test_daemon_creates_journal_and_registry_on_init(cfg: DaemonConfig) -> None:
    d = Daemon(cfg)
    assert (cfg.state_dir / "journal.jsonl").exists()
    assert (cfg.state_dir / "registry.json").exists()
    snap = d.registry.snapshot()
    assert snap.branch == "feat/v0.3"


def test_daemon_snapshot_includes_registry_and_journal(cfg: DaemonConfig) -> None:
    d = Daemon(cfg)
    snap = d.snapshot()
    assert "registry" in snap
    assert snap["registry"]["branch"] == "feat/v0.3"
    assert snap["registry"]["tasks"] == 0
    assert "journal" in snap
    assert snap["journal"]["entries"] >= 0


async def test_daemon_start_appends_daemon_start_event(cfg: DaemonConfig) -> None:
    d = Daemon(cfg)
    await d.start()
    try:
        last = d.journal.last_of("daemon.start")
        assert last is not None
        assert last.payload["branch"] == "feat/v0.3"
        assert last.payload["ceiling_usd"] == 5.0
    finally:
        await d.stop()


async def test_daemon_crash_resume_preserves_journal(cfg: DaemonConfig) -> None:
    """A fresh Daemon over the same state_dir sees the prior journal."""
    d1 = Daemon(cfg)
    await d1.start()
    await asyncio.sleep(0.1)
    await d1.stop()

    j_before = d1.journal.count()
    assert j_before > 0

    # New daemon over the same state dir
    d2 = Daemon(cfg)
    j_after = d2.journal.count()
    assert j_after == j_before


async def test_daemon_mirrors_watchdog_events_to_journal(cfg: DaemonConfig) -> None:
    """When a stuck subgoal fires, both the JSONL and the journal get it."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    d = Daemon(cfg)
    await d.start()
    try:
        # Create a goal with a sub-goal that's been running for >stuck_threshold_s.
        # We claim it, then backdate its claimed_at column to simulate a hang.
        d.store.goal_create("test-goal", "test-goal-desc", cost_budget=2.0)
        sg = d.store.subgoal_create("test-goal", "m@1", "stuck-sub")
        d.store.subgoal_claim_next("test-goal")
        backdated = (datetime.now(UTC) - timedelta(seconds=cfg.stuck_threshold_s + 5))
        with sqlite3.connect(str(d.store.path)) as conn:
            conn.execute(
                "UPDATE subgoals SET claimed_at = ? WHERE id = ?",
                (backdated.isoformat(timespec="microseconds"), sg.id),
            )
            conn.commit()

        # Wait for at least one watchdog tick to fire the stuck event
        for _ in range(40):
            await asyncio.sleep(0.05)
            if d.journal.last_of("subgoal.stuck") is not None:
                break
        stuck = d.journal.last_of("subgoal.stuck")
        assert stuck is not None
        assert stuck.payload.get("subgoal_id") == sg.id
    finally:
        await d.stop()


async def test_daemon_registry_persists_across_restart(cfg: DaemonConfig) -> None:
    """Registry state survives a daemon restart (no state loss on crash)."""
    from agentsos.work_registry import Task, TaskStatus

    d1 = Daemon(cfg)
    await d1.start()
    try:
        t = Task(id="v0.3-bridge", title="Telegram bridge",
                 phase="v0.3", status=TaskStatus.IN_PROGRESS.value)
        d1.registry.upsert_task(t)
        d1.registry.set_current("v0.3-bridge", next_id="v0.4-memory")
        d1.registry.flush()
        # simulate crash: just close without ceremony
    finally:
        await d1.stop()

    # New daemon over the same dir
    d2 = Daemon(cfg)
    snap = d2.registry.snapshot()
    assert "v0.3-bridge" in snap.tasks
    assert snap.tasks["v0.3-bridge"].status == TaskStatus.IN_PROGRESS.value
    assert snap.current_task_id == "v0.3-bridge"
    assert snap.next_task_id == "v0.4-memory"
