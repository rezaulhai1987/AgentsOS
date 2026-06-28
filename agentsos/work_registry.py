"""Crash-resilient work journal + live registry.

Two small modules that together solve the operator's #1 worry:
"what if the PC dies mid-build?"

  - `Journal` is an append-only JSONL log of every event the agent
    cares about: test runs, commits, PR opens/closes, plan checkpoints,
    custom markers. Each line has: ts, kind, payload. The journal is
    the source of truth for "what happened" — never rewritten.

  - `Registry` is a single JSON snapshot of the *current state*:
    which task is in progress, what's queued, what's blocked, what's
    done. The registry is the source of truth for "where are we
    right now" — rewritten atomically (tmp + os.replace) on every
    update so a crash mid-write never leaves a corrupt file.

`compute_next_actions` reads both, identifies the last in-progress
task, and returns an ordered list of "next actions" the agent
should take. On startup, the agent calls it and self-prompts
through the list. On exit (clean or crash), the journal is intact
and the registry reflects the last known state.

Both live under `<state_dir>/journal.jsonl` and `<state_dir>/registry.json`.
The Telegram bridge exposes both via `/where` (current registry)
and `/log` (tail of the journal).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


# --- journal ----------------------------------------------------------

class JournalKind(str, Enum):
    PLAN = "plan"                      # high-level plan checkpoint
    TASK_START = "task.start"
    TASK_DONE = "task.done"
    TASK_FAILED = "task.failed"
    TEST_RUN = "test.run"
    LINT_RUN = "lint.run"
    COMMIT = "commit"
    PR_OPEN = "pr.open"
    PR_MERGE = "pr.merge"
    PR_CLOSE = "pr.close"
    DAEMON_START = "daemon.start"
    DAEMON_STOP = "daemon.stop"
    TELEGRAM_SEND = "telegram.send"
    HEARTBEAT = "heartbeat"
    CUSTOM = "custom"


@dataclass
class JournalEntry:
    ts: str
    kind: str
    payload: dict[str, Any]
    seq: int = 0


class Journal:
    """Append-only JSONL. Never rewritten. Cheap to tail."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._seq = self._count_existing()

    def _count_existing(self) -> int:
        if not self.path.exists():
            return 0
        n = 0
        with self.path.open("rb") as f:
            for _ in f:
                n += 1
        return n

    def append(self, kind: str | JournalKind, payload: dict[str, Any] | None = None,
               **extra: Any) -> JournalEntry:
        payload = dict(payload or {})
        payload.update(extra)
        # Important: enum.str() yields "JournalKind.X", not the value.
        # Always serialize the value so readers can filter by `kind`.
        kind_str = kind.value if isinstance(kind, JournalKind) else str(kind)
        entry = JournalEntry(
            ts=datetime.now(UTC).isoformat(timespec="microseconds"),
            kind=kind_str,
            payload=payload,
            seq=self._seq + 1,
        )
        line = json.dumps(
            {"ts": entry.ts, "kind": entry.kind, "seq": entry.seq, **entry.payload},
            default=str,
        )
        # Append atomically: O_APPEND + single write + fsync.
        # Append-only files don't have the rename race, but flushing
        # + fsync matters when the journal is the crash-recovery spine.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._seq += 1
        return entry

    def tail(self, n: int = 50, kinds: Iterable[str] | None = None) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        wanted = set(kinds) if kinds is not None else None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        lines = text.splitlines()
        window = lines[-max(n * 4, n) :] if wanted else lines[-n:]
        out: list[JournalEntry] = []
        for line in window:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wanted is not None and obj.get("kind") not in wanted:
                continue
            out.append(JournalEntry(
                ts=obj.get("ts", ""),
                kind=obj.get("kind", ""),
                payload={k: v for k, v in obj.items() if k not in ("ts", "kind", "seq")},
                seq=int(obj.get("seq", 0)),
            ))
        return out[-n:]

    def last_of(self, kind: str | JournalKind) -> JournalEntry | None:
        entries = self.tail(n=200, kinds=[str(kind.value if isinstance(kind, JournalKind) else kind)])
        return entries[-1] if entries else None

    def count(self) -> int:
        return self._seq


# --- registry ---------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class Task:
    id: str
    title: str
    status: str = TaskStatus.PENDING.value
    phase: str = ""
    branch: str = ""
    pr_url: str = ""
    last_commit: str = ""
    last_test_run: str = ""
    tests_total: int = 0
    tests_passed: int = 0
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    notes: str = ""
    deps: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "last_commit": self.last_commit,
            "last_test_run": self.last_test_run,
            "tests_total": self.tests_total,
            "tests_passed": self.tests_passed,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "notes": self.notes,
            "deps": list(self.deps),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            status=d.get("status", TaskStatus.PENDING.value),
            phase=d.get("phase", ""),
            branch=d.get("branch", ""),
            pr_url=d.get("pr_url", ""),
            last_commit=d.get("last_commit", ""),
            last_test_run=d.get("last_test_run", ""),
            tests_total=int(d.get("tests_total", 0)),
            tests_passed=int(d.get("tests_passed", 0)),
            started_at=d.get("started_at", ""),
            updated_at=d.get("updated_at", ""),
            finished_at=d.get("finished_at", ""),
            notes=d.get("notes", ""),
            deps=list(d.get("deps", [])),
            metadata=dict(d.get("metadata", {})),
        )


@dataclass
class RegistrySnapshot:
    agent: str
    project: str
    branch: str
    started_at: str
    updated_at: str
    current_task_id: str
    next_task_id: str
    tasks: dict[str, Task]
    head_commit: str = ""
    prs_open: list[str] = field(default_factory=list)
    test_run_total: int = 0
    test_run_passed: int = 0
    daemon_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "project": self.project,
            "branch": self.branch,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_task_id": self.current_task_id,
            "next_task_id": self.next_task_id,
            "head_commit": self.head_commit,
            "prs_open": list(self.prs_open),
            "test_run_total": self.test_run_total,
            "test_run_passed": self.test_run_passed,
            "daemon_state": dict(self.daemon_state),
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegistrySnapshot":
        return cls(
            agent=d.get("agent", "agentsos-builder"),
            project=d.get("project", "AgentsOS"),
            branch=d.get("branch", ""),
            started_at=d.get("started_at", datetime.now(UTC).isoformat()),
            updated_at=d.get("updated_at", datetime.now(UTC).isoformat()),
            current_task_id=d.get("current_task_id", ""),
            next_task_id=d.get("next_task_id", ""),
            tasks={tid: Task.from_dict(t) for tid, t in d.get("tasks", {}).items()},
            head_commit=d.get("head_commit", ""),
            prs_open=list(d.get("prs_open", [])),
            test_run_total=int(d.get("test_run_total", 0)),
            test_run_passed=int(d.get("test_run_passed", 0)),
            daemon_state=dict(d.get("daemon_state", {})),
        )


class Registry:
    """Single-JSON atomic-write snapshot of the build state."""

    def __init__(self, path: Path, agent: str = "agentsos-builder",
                 project: str = "AgentsOS", branch: str = "") -> None:
        self.path = path
        self.bak_path = path.with_suffix(path.suffix + ".bak")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent = agent
        self.project = project
        self.branch = branch
        self._snapshot = self._load_or_init()
        # Always flush once on init so the file exists from the moment
        # the Registry is created. This makes crash-resume trivial:
        # there's always a snapshot on disk to fall back to.
        if not self.path.exists():
            self.flush()

    def _load_or_init(self) -> RegistrySnapshot:
        for path in (self.path, self.bak_path):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                snap = RegistrySnapshot.from_dict(data)
                self.branch = snap.branch or self.branch
                return snap
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                continue
        return RegistrySnapshot(
            agent=self.agent,
            project=self.project,
            branch=self.branch,
            started_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            updated_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            current_task_id="",
            next_task_id="",
            tasks={},
        )

    # --- task API ---

    def upsert_task(self, task: Task) -> None:
        if not task.id:
            raise ValueError("task.id required")
        if not task.started_at:
            task.started_at = datetime.now(UTC).isoformat(timespec="microseconds")
        task.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
        self._snapshot.tasks[task.id] = task

    def mark_status(self, task_id: str, status: str, notes: str = "") -> None:
        t = self._snapshot.tasks.get(task_id)
        if t is None:
            raise KeyError(f"unknown task {task_id}")
        t.status = status
        if notes:
            t.notes = notes
        t.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
        if status == TaskStatus.DONE.value and not t.finished_at:
            t.finished_at = t.updated_at

    def set_test_run(self, task_id: str, passed: int, total: int) -> None:
        t = self._snapshot.tasks.get(task_id)
        if t is None:
            return
        t.tests_passed = passed
        t.tests_total = total
        t.last_test_run = datetime.now(UTC).isoformat(timespec="microseconds")
        t.updated_at = t.last_test_run

    def set_pr(self, task_id: str, pr_url: str) -> None:
        t = self._snapshot.tasks.get(task_id)
        if t is None:
            return
        t.pr_url = pr_url
        if pr_url not in self._snapshot.prs_open:
            self._snapshot.prs_open.append(pr_url)
        t.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")

    def set_commit(self, task_id: str, commit_sha: str) -> None:
        t = self._snapshot.tasks.get(task_id)
        if t is None:
            return
        t.last_commit = commit_sha
        t.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
        self._snapshot.head_commit = commit_sha

    def set_current(self, task_id: str, next_id: str = "") -> None:
        self._snapshot.current_task_id = task_id
        self._snapshot.next_task_id = next_id

    def set_head_commit(self, sha: str) -> None:
        self._snapshot.head_commit = sha
        self._snapshot.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")

    # --- snapshot lifecycle ---

    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    def flush(self) -> None:
        self._snapshot.updated_at = datetime.now(UTC).isoformat(timespec="microseconds")
        data = json.dumps(self._snapshot.to_dict(), indent=2, default=str)
        # Atomic write: tmp in same dir, then os.replace. Rotate
        # previous good snapshot -> bak so a future crash mid-write
        # still leaves a usable file.
        dirpath = self.path.parent
        fd, tmp_path = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=dirpath)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if self.path.exists():
            try:
                self.path.replace(self.bak_path)
            except OSError:
                pass
        os.replace(tmp_path, self.path)


# --- resume helper ---------------------------------------------------

@dataclass
class NextAction:
    task_id: str
    title: str
    status: str
    reason: str
    phase: str = ""
    pr_url: str = ""


def compute_next_actions(reg: Registry, journal: Journal | None = None,
                         max_items: int = 5) -> list[NextAction]:
    """Return the ordered list of next actions the agent should take.

    Order of preference:
      1. The current_task_id if it exists and is in_progress
      2. Any in_progress task not yet marked done (orphans from crash)
      3. Failed tasks with no follow-up (re-attempt)
      4. Pending tasks with all deps satisfied
      5. Any remaining pending tasks
      6. Blocked (lowest priority; surface for the operator)
    """
    snap = reg.snapshot()
    actions: list[NextAction] = []
    seen: set[str] = set()

    def push(t: Task, reason: str) -> None:
        if t.id in seen:
            return
        seen.add(t.id)
        actions.append(NextAction(
            task_id=t.id,
            title=t.title,
            status=t.status,
            reason=reason,
            phase=t.phase,
            pr_url=t.pr_url,
        ))

    if snap.current_task_id and snap.current_task_id in snap.tasks:
        t = snap.tasks[snap.current_task_id]
        if t.status == TaskStatus.IN_PROGRESS.value:
            push(t, "resume: this was the in-progress task at last save")

    for t in snap.tasks.values():
        if t.status == TaskStatus.IN_PROGRESS.value and t.id not in seen:
            push(t, "resume: orphaned in-progress from previous run")

    for t in snap.tasks.values():
        if t.status == TaskStatus.FAILED.value and t.id not in seen:
            push(t, "retry: failed in a previous run")

    for t in snap.tasks.values():
        if t.status != TaskStatus.PENDING.value or t.id in seen:
            continue
        if all(snap.tasks.get(d) and snap.tasks[d].status == TaskStatus.DONE.value for d in t.deps):
            push(t, "next pending: deps satisfied")

    for t in snap.tasks.values():
        if t.status == TaskStatus.PENDING.value and t.id not in seen:
            push(t, "queue: pending (deps unsatisfied)")

    for t in snap.tasks.values():
        if t.status == TaskStatus.BLOCKED.value and t.id not in seen:
            push(t, "blocked: needs operator")

    return actions[:max_items]


def render_registry(reg: Registry, actions: list[NextAction] | None = None) -> str:
    """Plain-text registry dump (used by the daemon's snapshot and
    by the Telegram `/where` command)."""
    snap = reg.snapshot()
    lines: list[str] = []
    lines.append(f"REGISTRY // {snap.project} @ {snap.branch}")
    lines.append(f"updated_at = {snap.updated_at}")
    lines.append(f"head_commit = {snap.head_commit}")
    lines.append(f"current_task = {snap.current_task_id or '(none)'}")
    lines.append(f"prs_open = {len(snap.prs_open)}")
    lines.append("")
    lines.append("TASKS:")
    for t in snap.tasks.values():
        marker = {
            TaskStatus.PENDING.value: "[ ]",
            TaskStatus.IN_PROGRESS.value: "[>]",
            TaskStatus.DONE.value: "[x]",
            TaskStatus.FAILED.value: "[!]",
            TaskStatus.BLOCKED.value: "[B]",
        }.get(t.status, "[?]")
        line = f"  {marker} {t.id} {t.title} [{t.phase or '—'}]"
        if t.pr_url:
            line += f"  pr={t.pr_url}"
        if t.tests_total:
            line += f"  tests={t.tests_passed}/{t.tests_total}"
        lines.append(line)
    if actions:
        lines.append("")
        lines.append("NEXT ACTIONS:")
        for a in actions:
            lines.append(f"  → {a.task_id}  {a.title}  [{a.status}]  — {a.reason}")
    return "\n".join(lines)