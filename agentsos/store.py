"""SQLite-backed persistent goal store for AgentsOS.

The store is the spine of the v0.3 always-on OS layer. Every goal,
subgoal, and cost record the daemon observes lives in this SQLite file.
The store is intentionally a thin layer over raw `sqlite3` — no ORM,
no migration framework — because we want to be able to inspect, back
up, and replay the DB with `sqlite3` from the shell.

Schema:
  - goals         — top-level operator objectives
  - subgoals      — atomic units of work, each backed by a manifest
  - cost_ledger   — append-only log of (run, agent, tokens, cost)
  - dlq           — dead-letter queue: subgoals that exhausted retries
  - chat_state    — per-Telegram-chat memory (v0.5+)

Concurrency: WAL mode + `busy_timeout=5000`. The daemon runs the
watchdog, reactor, and Telegram bridge in one asyncio loop; multiple
coroutines may touch the store simultaneously. SQLite serialises
writes, so a `BEGIN IMMEDIATE` is used for the hot path
(subgoal_claim_next) so the read+update is atomic.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

GoalStatus = Literal["active", "paused", "done", "failed", "partial"]
SubgoalStatus = Literal["pending", "running", "done", "failed"]

# Schema version. Bumped by `migrate()` when migrations are added.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    cost_budget   REAL NOT NULL DEFAULT 1.0,
    daily_ceiling REAL,
    deadline      TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    parent_id     TEXT
);
CREATE INDEX IF NOT EXISTS goals_status_idx ON goals(status);

CREATE TABLE IF NOT EXISTS subgoals (
    id              TEXT PRIMARY KEY,
    goal_id         TEXT NOT NULL,
    manifest_id     TEXT NOT NULL,
    input           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    output          TEXT,
    checkpoint_path TEXT,
    depends_on      TEXT NOT NULL DEFAULT '[]',
    claimed_at      TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT,
    FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE INDEX IF NOT EXISTS subgoals_goal_idx ON subgoals(goal_id, status);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    goal_id     TEXT,
    subgoal_id  TEXT,
    tokens_in   INTEGER NOT NULL,
    tokens_out  INTEGER NOT NULL,
    cost_usd    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cost_ledger_ts_idx ON cost_ledger(ts);
CREATE INDEX IF NOT EXISTS cost_ledger_goal_idx ON cost_ledger(goal_id);

CREATE TABLE IF NOT EXISTS dlq (
    id          TEXT PRIMARY KEY,
    subgoal_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    parked_at   TEXT NOT NULL,
    replayed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id     TEXT PRIMARY KEY,
    last_cmd    TEXT,
    last_msgs   TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Goal:
    id: str
    name: str
    description: str
    status: GoalStatus
    cost_budget: float
    daily_ceiling: float | None
    deadline: str | None
    created_at: str
    finished_at: str | None = None
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "cost_budget": self.cost_budget,
            "daily_ceiling": self.daily_ceiling,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Goal:
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            cost_budget=row["cost_budget"],
            daily_ceiling=row["daily_ceiling"],
            deadline=row["deadline"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            parent_id=row["parent_id"],
        )


@dataclass
class Subgoal:
    id: str
    goal_id: str
    manifest_id: str
    input: str
    status: SubgoalStatus
    attempts: int
    last_error: str | None
    output: str | None
    checkpoint_path: str | None
    depends_on: list[str] = field(default_factory=list)
    claimed_at: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "manifest_id": self.manifest_id,
            "input": self.input,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "output": self.output,
            "checkpoint_path": self.checkpoint_path,
            "depends_on": self.depends_on,
            "claimed_at": self.claimed_at,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Subgoal:
        return cls(
            id=row["id"],
            goal_id=row["goal_id"],
            manifest_id=row["manifest_id"],
            input=row["input"],
            status=row["status"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            output=row["output"],
            checkpoint_path=row["checkpoint_path"],
            depends_on=json.loads(row["depends_on"] or "[]"),
            claimed_at=row["claimed_at"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )


@dataclass
class CostRecord:
    id: int
    ts: str
    run_id: str
    agent_name: str
    goal_id: str | None
    subgoal_id: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "goal_id": self.goal_id,
            "subgoal_id": self.subgoal_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
        }


class Store:
    """Persistent goal / subgoal / cost store backed by SQLite.

    One instance per daemon. Thread-safe via SQLite's own serialisation
    (WAL + busy_timeout); safe to call from multiple asyncio tasks as
    long as you don't share a connection across coroutines (we open
    a fresh connection per call).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # `check_same_thread=False` lets us pass connections across the
        # asyncio-thread boundary in tests; the store itself never
        # holds a connection between calls.
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.executescript(_SCHEMA)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            cur = conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

    # --- goals ---

    def goal_create(
        self,
        name: str,
        description: str,
        cost_budget: float = 1.0,
        daily_ceiling: float | None = None,
        deadline: str | None = None,
        parent_id: str | None = None,
        goal_id: str | None = None,
    ) -> Goal:
        gid = goal_id or _new_id("goal")
        with self._connect() as conn:
            with self._tx(conn):
                conn.execute(
                    "INSERT INTO goals (id, name, description, status, cost_budget, "
                    "daily_ceiling, deadline, created_at, parent_id) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                    (
                        gid,
                        name,
                        description,
                        cost_budget,
                        daily_ceiling,
                        deadline,
                        _now(),
                        parent_id,
                    ),
                )
        return self.goal_get(gid)

    def goal_get(self, goal_id: str) -> Goal:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown goal: {goal_id}")
        return Goal.from_row(row)

    def goal_list(
        self, status: GoalStatus | None = None, limit: int = 100
    ) -> list[Goal]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM goals ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM goals WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
        return [Goal.from_row(r) for r in rows]

    def goal_update_status(
        self, goal_id: str, status: GoalStatus, finished: bool = False
    ) -> None:
        with self._connect() as conn:
            with self._tx(conn):
                if finished:
                    conn.execute(
                        "UPDATE goals SET status = ?, finished_at = ? WHERE id = ?",
                        (status, _now(), goal_id),
                    )
                else:
                    conn.execute(
                        "UPDATE goals SET status = ? WHERE id = ?", (status, goal_id)
                    )

    # --- subgoals ---

    def subgoal_create(
        self,
        goal_id: str,
        manifest_id: str,
        input: str,
        depends_on: list[str] | None = None,
        subgoal_id: str | None = None,
    ) -> Subgoal:
        sid = subgoal_id or _new_id("sg")
        with self._connect() as conn:
            with self._tx(conn):
                conn.execute(
                    "INSERT INTO subgoals (id, goal_id, manifest_id, input, status, "
                    "attempts, depends_on, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                    (
                        sid,
                        goal_id,
                        manifest_id,
                        input,
                        json.dumps(depends_on or []),
                        _now(),
                    ),
                )
        return self.subgoal_get(sid)

    def subgoal_get(self, subgoal_id: str) -> Subgoal:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subgoals WHERE id = ?", (subgoal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown subgoal: {subgoal_id}")
        return Subgoal.from_row(row)

    def subgoal_list(
        self,
        goal_id: str | None = None,
        status: SubgoalStatus | None = None,
        limit: int = 200,
    ) -> list[Subgoal]:
        clauses: list[str] = []
        params: list[Any] = []
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM subgoals {where} ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [Subgoal.from_row(r) for r in rows]

    def subgoal_claim_next(self, goal_id: str) -> Subgoal | None:
        """Atomically claim the next pending subgoal for the goal.

        Returns the claimed subgoal (status=pending → running, attempts
        incremented, claimed_at set) or None if no pending subgoals
        remain or all have unfulfilled dependencies.

        Uses BEGIN IMMEDIATE so two concurrent claimers cannot both
        get the same row.
        """
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT * FROM subgoals WHERE goal_id = ? AND status = 'pending' "
                    "ORDER BY created_at ASC",
                    (goal_id,),
                ).fetchall()
                for row in rows:
                    sg = Subgoal.from_row(row)
                    deps_met = self._deps_met(conn, sg.goal_id, sg.depends_on)
                    if not deps_met:
                        continue
                    conn.execute(
                        "UPDATE subgoals SET status = 'running', "
                        "attempts = attempts + 1, claimed_at = ? WHERE id = ?",
                        (_now(), sg.id),
                    )
                    conn.commit()
                    return self.subgoal_get(sg.id)
                conn.commit()
                return None
            except Exception:
                conn.rollback()
                raise

    def _deps_met(
        self, conn: sqlite3.Connection, goal_id: str, depends_on: list[str]
    ) -> bool:
        if not depends_on:
            return True
        placeholders = ",".join("?" * len(depends_on))
        rows = conn.execute(
            f"SELECT id, status FROM subgoals "
            f"WHERE goal_id = ? AND id IN ({placeholders})",
            (goal_id, *depends_on),
        ).fetchall()
        if len(rows) != len(depends_on):
            return False  # missing dependency
        return all(r["status"] == "done" for r in rows)

    def subgoal_complete(
        self,
        subgoal_id: str,
        output: str,
        checkpoint_path: str | None = None,
    ) -> None:
        with self._connect() as conn:
            with self._tx(conn):
                conn.execute(
                    "UPDATE subgoals SET status = 'done', output = ?, "
                    "checkpoint_path = ?, finished_at = ? WHERE id = ?",
                    (output, checkpoint_path, _now(), subgoal_id),
                )

    def subgoal_fail(self, subgoal_id: str, error: str) -> None:
        with self._connect() as conn:
            with self._tx(conn):
                conn.execute(
                    "UPDATE subgoals SET status = 'pending', last_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (error, subgoal_id),
                )

    def subgoal_park(self, subgoal_id: str, error: str) -> None:
        """Park a subgoal in DLQ — used when retries are exhausted."""
        with self._connect() as conn:
            with self._tx(conn):
                conn.execute(
                    "UPDATE subgoals SET status = 'failed', last_error = ?, "
                    "finished_at = ? WHERE id = ?",
                    (error, _now(), subgoal_id),
                )
                conn.execute(
                    "INSERT INTO dlq (id, subgoal_id, reason, parked_at) "
                    "VALUES (?, ?, ?, ?)",
                    (_new_id("dlq"), subgoal_id, error, _now()),
                )

    # --- cost ledger ---

    def cost_record(
        self,
        run_id: str,
        agent_name: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        goal_id: str | None = None,
        subgoal_id: str | None = None,
    ) -> CostRecord:
        with self._connect() as conn:
            with self._tx(conn):
                cur = conn.execute(
                    "INSERT INTO cost_ledger (ts, run_id, agent_name, goal_id, "
                    "subgoal_id, tokens_in, tokens_out, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        run_id,
                        agent_name,
                        goal_id,
                        subgoal_id,
                        tokens_in,
                        tokens_out,
                        cost_usd,
                    ),
                )
                row_id = cur.lastrowid
        return self.cost_get(row_id)

    def cost_get(self, record_id: int) -> CostRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cost_ledger WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown cost record: {record_id}")
        return CostRecord(
            id=row["id"],
            ts=row["ts"],
            run_id=row["run_id"],
            agent_name=row["agent_name"],
            goal_id=row["goal_id"],
            subgoal_id=row["subgoal_id"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_usd=row["cost_usd"],
        )

    def cost_sum(
        self,
        goal_id: str | None = None,
        since: str | None = None,
    ) -> tuple[int, int, float]:
        """Return (tokens_in, tokens_out, cost_usd) over the matching rows.

        `since` is an ISO timestamp; only records with ts >= since count.
        Used by the cost guard for daily ceilings.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if goal_id is not None:
            clauses.append("goal_id = ?")
            params.append(goal_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(tokens_in), 0) AS tin, "
                f"COALESCE(SUM(tokens_out), 0) AS tout, "
                f"COALESCE(SUM(cost_usd), 0.0) AS cost "
                f"FROM cost_ledger {where}",
                params,
            ).fetchone()
        return int(row["tin"]), int(row["tout"]), float(row["cost"])

    # --- dlq ---

    def dlq_list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dlq ORDER BY parked_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- healthcheck ---

    def healthcheck(self) -> dict[str, Any]:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        _, _, today_cost = self.cost_sum(since=today.isoformat())
        with self._connect() as conn:
            active_goals = conn.execute(
                "SELECT COUNT(*) AS c FROM goals WHERE status = 'active'"
            ).fetchone()["c"]
            pending_subgoals = conn.execute(
                "SELECT COUNT(*) AS c FROM subgoals WHERE status = 'pending'"
            ).fetchone()["c"]
            running_subgoals = conn.execute(
                "SELECT COUNT(*) AS c FROM subgoals WHERE status = 'running'"
            ).fetchone()["c"]
            failed_subgoals = conn.execute(
                "SELECT COUNT(*) AS c FROM subgoals WHERE status = 'failed'"
            ).fetchone()["c"]
            dlq_count = conn.execute("SELECT COUNT(*) AS c FROM dlq").fetchone()["c"]
        return {
            "active_goals": active_goals,
            "pending_subgoals": pending_subgoals,
            "running_subgoals": running_subgoals,
            "failed_subgoals": failed_subgoals,
            "dlq_entries": dlq_count,
            "cost_today_usd": round(today_cost, 6),
        }

    # --- shutdown ---

    def close(self) -> None:
        # No persistent connection held; nothing to do. Kept for symmetry
        # with future pooled implementations.
        pass
