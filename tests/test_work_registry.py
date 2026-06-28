"""Tests for the crash-resilient work journal + live registry (v0.3.6).

The journal is append-only JSONL. The registry is atomic-write JSON.
Resume picks the next action based on current_task, in-progress
orphans, failed retries, pending-with-deps-satisfied, then pending.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentsos.work_registry import (
    Journal,
    JournalKind,
    NextAction,
    Registry,
    Task,
    TaskStatus,
    compute_next_actions,
    render_registry,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def journal(state_dir: Path) -> Journal:
    return Journal(state_dir / "journal.jsonl")


@pytest.fixture
def registry(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Registry:
    monkeypatch.chdir(state_dir)
    return Registry(state_dir / "registry.json", branch="feat/v0.3")


# --- journal ----------------------------------------------------------

def test_journal_appends_in_order(journal: Journal) -> None:
    a = journal.append(JournalKind.TASK_START, {"id": "t1"})
    b = journal.append(JournalKind.TASK_DONE, {"id": "t1"})
    assert a.seq == 1
    assert b.seq == 2
    assert b.ts > a.ts


def test_journal_tail_filters_by_kind(journal: Journal) -> None:
    journal.append(JournalKind.TASK_START, {"id": "t1"})
    journal.append(JournalKind.HEARTBEAT)
    journal.append(JournalKind.TASK_DONE, {"id": "t1"})
    journal.append(JournalKind.HEARTBEAT)
    tail = journal.tail(n=10, kinds=[JournalKind.TASK_DONE.value])
    assert len(tail) == 1
    assert tail[0].payload.get("id") == "t1"


def test_journal_last_of_returns_latest_match(journal: Journal) -> None:
    journal.append(JournalKind.TASK_START, {"id": "t1"})
    journal.append(JournalKind.TASK_FAILED, {"id": "t1", "err": "boom"})
    last = journal.last_of(JournalKind.TASK_FAILED.value)
    assert last is not None
    assert last.payload["err"] == "boom"


def test_journal_survives_crash_simulated(journal: Journal, state_dir: Path) -> None:
    """If the process dies after the first write, the entry is still there."""
    journal.append(JournalKind.TASK_START, {"id": "t1"})
    # Simulate restart: new Journal over the same file
    j2 = Journal(state_dir / "journal.jsonl")
    assert j2.count() == 1
    j2.append(JournalKind.TASK_DONE, {"id": "t1"})
    assert j2.count() == 2


def test_journal_atomic_append_under_tmp(journal: Journal, state_dir: Path) -> None:
    journal.append("custom.kind", {"x": 1})
    journal.append("custom.kind", {"x": 2})
    # No leftover .tmp files (we use O_APPEND, not tmp-then-rename)
    leftovers = list(state_dir.glob("*.tmp"))
    assert leftovers == []


# --- registry ---------------------------------------------------------

def test_registry_creates_initial_snapshot(state_dir: Path) -> None:
    r = Registry(state_dir / "reg.json", branch="main")
    snap = r.snapshot()
    assert snap.branch == "main"
    assert snap.tasks == {}
    assert r.path.exists()


def test_registry_upsert_and_mark_done(registry: Registry) -> None:
    t = Task(id="t1", title="write store", phase="v0.3")
    registry.upsert_task(t)
    registry.set_current("t1", next_id="t2")
    registry.mark_status("t1", TaskStatus.IN_PROGRESS.value)
    registry.flush()

    # Simulate restart
    r2 = Registry(registry.path, branch="feat/v0.3")
    snap = r2.snapshot()
    assert "t1" in snap.tasks
    assert snap.tasks["t1"].status == TaskStatus.IN_PROGRESS.value
    assert snap.current_task_id == "t1"


def test_registry_atomic_write_keeps_backup(registry: Registry, state_dir: Path) -> None:
    registry.upsert_task(Task(id="t1", title="x"))
    registry.flush()
    first = registry.path.read_bytes()
    bak = state_dir / "registry.json.bak"
    # After second flush, the first file is preserved as .bak
    registry.upsert_task(Task(id="t2", title="y"))
    registry.flush()
    assert bak.exists()
    assert registry.path.read_bytes() != first


def test_registry_recovers_from_corrupted_main_uses_backup(state_dir: Path) -> None:
    main = state_dir / "reg.json"
    bak = main.with_suffix(".json.bak")
    main.write_text("not-json", encoding="utf-8")
    bak.write_text(json.dumps({
        "agent": "a", "project": "P", "branch": "main",
        "started_at": "2026-06-28T00:00:00Z",
        "updated_at": "2026-06-28T00:00:00Z",
        "current_task_id": "x", "next_task_id": "",
        "tasks": {"x": {"id": "x", "title": "from-backup", "status": "in_progress"}},
        "head_commit": "", "prs_open": [], "test_run_total": 0,
        "test_run_passed": 0, "daemon_state": {},
    }), encoding="utf-8")
    r = Registry(main)
    assert r.snapshot().tasks["x"].title == "from-backup"


def test_registry_test_run_and_pr(registry: Registry) -> None:
    registry.upsert_task(Task(id="t1", title="x"))
    registry.set_test_run("t1", passed=15, total=15)
    registry.set_pr("t1", "https://github.com/x/y/pull/4")
    registry.flush()
    snap = registry.snapshot()
    assert snap.tasks["t1"].tests_passed == 15
    assert snap.tasks["t1"].tests_total == 15
    assert snap.tasks["t1"].pr_url == "https://github.com/x/y/pull/4"
    assert "https://github.com/x/y/pull/4" in snap.prs_open


# --- resume -----------------------------------------------------------

def test_resume_prefers_current_task(registry: Registry) -> None:
    registry.upsert_task(Task(id="t1", title="current", status=TaskStatus.IN_PROGRESS.value))
    registry.upsert_task(Task(id="t2", title="next", status=TaskStatus.PENDING.value))
    registry.set_current("t1", next_id="t2")
    actions = compute_next_actions(registry)
    assert actions[0].task_id == "t1"
    assert "current" in actions[0].reason.lower() or "resume" in actions[0].reason.lower()


def test_resume_picks_failed_before_pending(registry: Registry) -> None:
    registry.upsert_task(Task(id="t1", title="failed one", status=TaskStatus.FAILED.value))
    registry.upsert_task(Task(id="t2", title="pending", status=TaskStatus.PENDING.value))
    actions = compute_next_actions(registry)
    assert actions[0].task_id == "t1"
    assert "retry" in actions[0].reason


def test_resume_respects_deps(registry: Registry) -> None:
    registry.upsert_task(Task(id="t1", title="do-first", status=TaskStatus.DONE.value))
    registry.upsert_task(Task(id="t2", title="after-t1", status=TaskStatus.PENDING.value, deps=["t1"]))
    registry.upsert_task(Task(id="t3", title="after-t2", status=TaskStatus.PENDING.value, deps=["t2"]))
    actions = compute_next_actions(registry)
    assert actions[0].task_id == "t2"
    assert actions[1].task_id == "t3"


def test_resume_handles_orphaned_in_progress(registry: Registry) -> None:
    # No current_task_id, but a stray in_progress task from a crash
    registry.upsert_task(Task(id="t1", title="orphan", status=TaskStatus.IN_PROGRESS.value))
    actions = compute_next_actions(registry)
    assert actions[0].task_id == "t1"
    assert "orphan" in actions[0].reason


def test_resume_after_crash_returns_full_plan(registry: Registry) -> None:
    # Simulate a crash mid-v0.3: 1 done, 1 in_progress (orphaned), 1 pending
    registry.upsert_task(Task(id="v03-store", title="store", status=TaskStatus.DONE.value))
    registry.upsert_task(Task(id="v03-watchdog", title="watchdog",
                              status=TaskStatus.IN_PROGRESS.value))
    registry.upsert_task(Task(id="v03-costguard", title="costguard",
                              status=TaskStatus.PENDING.value))
    actions = compute_next_actions(registry, max_items=10)
    assert [a.task_id for a in actions] == ["v03-watchdog", "v03-costguard"]


def test_render_registry_is_human_readable(registry: Registry) -> None:
    registry.upsert_task(Task(id="t1", title="store", status=TaskStatus.DONE.value, phase="v0.3"))
    registry.upsert_task(Task(id="t2", title="watchdog", status=TaskStatus.IN_PROGRESS.value, phase="v0.3"))
    registry.upsert_task(Task(id="t3", title="costguard", status=TaskStatus.PENDING.value, phase="v0.3"))
    registry.set_current("t2", next_id="t3")
    text = render_registry(registry, compute_next_actions(registry))
    assert "REGISTRY" in text
    assert "[x] t1 store" in text
    assert "[>] t2 watchdog" in text
    assert "[ ] t3 costguard" in text
    assert "→ t2" in text  # next action
    assert "→ t3" in text