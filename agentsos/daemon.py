"""Always-on daemon — `agents daemon`.

The daemon is the spine of the v0.3+ OS. It:
  - Opens the persistent store at `state.db` (configurable).
  - Starts the watchdog (stuck detection, deadline sweep).
  - Subscribes a logger to all watchdog events so the JSONL log
    captures every transition.
  - Blocks on SIGINT/SIGTERM for a graceful shutdown.

In v0.4 the orchestrator will be plugged in here. In v0.5 the
Telegram bridge will be plugged in here. v0.6 will add the
decomposer. v0.7 will add the self-healing supervisor.

Each layer is added by appending a coroutine to `self._tasks` in
`start()` — the daemon never grows into a god-object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentsos.cost_guard import CostGuard
from agentsos.store import Store
from agentsos.watchdog import Watchdog
from agentsos.work_registry import Journal, Registry

log = logging.getLogger("agentsos.daemon")


@dataclass
class DaemonConfig:
    state_dir: Path
    daily_ceiling_usd: float = 50.0
    watchdog_interval_s: float = 30.0
    stuck_threshold_s: float = 600.0
    jsonl_log: Path | None = None
    # Git branch the daemon is operating under (used by the work registry).
    branch: str = ""
    # Extra async tasks to run alongside the watchdog. Each takes the
    # daemon as its only arg. v0.4+ will register the orchestrator here.
    extra_tasks: list[Callable[["Daemon"], Awaitable[None]]] = field(default_factory=list)


class Daemon:
    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        if config.jsonl_log is None:
            config.jsonl_log = config.state_dir / "daemon.jsonl"

        self.store = Store(config.state_dir / "state.db")
        self.cost_guard = CostGuard(self.store, daily_ceiling_usd=config.daily_ceiling_usd)
        self.watchdog = Watchdog(
            self.store,
            interval_s=config.watchdog_interval_s,
            stuck_threshold_s=config.stuck_threshold_s,
        )
        # Crash-resilient work journal + live registry (v0.3.6).
        # The journal is the spine for "what happened" and the
        # registry is the spine for "where are we right now". Both
        # live under <state_dir>/ so a `restart` resumes from disk.
        self.journal = Journal(config.state_dir / "journal.jsonl")
        self.registry = Registry(
            config.state_dir / "registry.json", branch=config.branch
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self.started_at: str | None = None
        self._log_fp = None  # type: ignore[assignment]​

    # --- event bus: watchdog + cost guard emit, daemon logs ---

    async def _log_event(self, topic: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            "topic": topic,
            **payload,
        }
        line = json.dumps(record, default=str)
        if self._log_fp is not None:
            self._log_fp.write(line + "\n")
            self._log_fp.flush()
        log.info("%s %s", topic, payload)
        # Mirror to the crash-resilient journal so a `tail -f` from
        # Telegram or a `Registry.compute_next_actions` after a crash
        # sees the same timeline.
        try:
            self.journal.append(topic, payload)
        except Exception as exc:  # pragma: no cover - journaling must never break the daemon
            log.warning("journal.append failed: %s", exc)

    def _attach_event_logging(self) -> None:
        # Always-on topics that should land in the JSONL.
        for topic in ("subgoal.stuck", "goal.deadline_missed", "cost.ceiling_breached"):
            self.watchdog.on(topic, lambda p, t=topic: self._log_event(t, p))
            self.cost_guard.on(topic, lambda p, t=topic: self._log_event(t, p))

    # --- lifecycle ---

    async def start(self) -> None:
        self._log_fp = open(self.config.jsonl_log, "a", encoding="utf-8")
        self._attach_event_logging()
        await self.watchdog.start()
        for factory in self.config.extra_tasks:
            self._tasks.append(asyncio.create_task(factory(self), name="agentsos.extra"))
        # Heartbeat task: writes daemon.heartbeat to JSONL every
        # watchdog_interval_s so the log shows the daemon is alive
        # even when no other events happen.
        self._heartbeat_task: asyncio.Task[None] | None = asyncio.create_task(
            self._heartbeat_loop(), name="agentsos.heartbeat"
        )
        self.started_at = datetime.now(UTC).isoformat(timespec="microseconds")
        self.journal.append("daemon.start", {"state_dir": str(self.config.state_dir),
                                              "branch": self.config.branch,
                                              "ceiling_usd": self.config.daily_ceiling_usd})
        log.info("daemon started (state_dir=%s)", self.config.state_dir)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self._log_event(
                "daemon.heartbeat",
                {"uptime_s": self.uptime_s(), "health": self.store.healthcheck()},
            )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.watchdog_interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def wait(self) -> None:
        """Block until stop() is called or a signal is received."""
        # Wire SIGINT/SIGTERM (Unix) and SIGBREAK (Windows).
        loop = asyncio.get_running_loop()
        handled: list[Callable[[Any], None]] = []

        def _shutdown(signame: str) -> None:
            log.info("received %s, shutting down", signame)
            self._stop.set()

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                handled.append(loop.add_signal_handler(sig, _shutdown, sig.name))
        except NotImplementedError:
            # Windows: signal handlers may not be installable in all loops.
            log.debug("signal handlers not installed (Windows or sandboxed)")

        try:
            await self._stop.wait()
        finally:
            for h in handled:
                try:
                    h()  # type: ignore[operator]
                except Exception:  # pragma: no cover
                    pass
            await self.stop()

    async def stop(self) -> None:
        log.info("daemon stopping")
        # Cancel heartbeat first so it stops writing to the log before close.
        heartbeat = getattr(self, "_heartbeat_task", None)
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
        self._heartbeat_task = None  # type: ignore[assignment]

        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self.watchdog.stop()
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None
        self.store.close()

    def uptime_s(self) -> float:
        if self.started_at is None:
            return 0.0
        start = datetime.fromisoformat(self.started_at)
        return (datetime.now(UTC) - start).total_seconds()

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "uptime_s": self.uptime_s(),
            "state_dir": str(self.config.state_dir),
            "watchdog": self.watchdog.stats(),
            "cost_guard": self.cost_guard.snapshot(),
            "store_health": self.store.healthcheck(),
            "registry": {
                "branch": self.registry.snapshot().branch,
                "head_commit": self.registry.snapshot().head_commit,
                "current_task_id": self.registry.snapshot().current_task_id,
                "next_task_id": self.registry.snapshot().next_task_id,
                "tasks": len(self.registry.snapshot().tasks),
                "prs_open": len(self.registry.snapshot().prs_open),
            },
            "journal": {"entries": self.journal.count()},
        }