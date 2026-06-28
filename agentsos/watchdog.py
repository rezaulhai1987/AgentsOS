"""Always-on watchdog — heartbeat, stuck detection, deadline sweep.

Runs as a single asyncio task. Every `interval_s` seconds it:

  1. Finds subgoals in `running` state whose `claimed_at` is older than
     `stuck_threshold_s`. Marks them failed (`error="watchdog: stuck"`)
     and emits a `subgoal.stuck` event so the reactor + Telegram
     alerts can react.

  2. Finds goals whose `deadline` has passed. Marks them failed and
     emits `goal.deadline_missed`.

The watchdog never raises. If the store is momentarily unavailable
(e.g. DB locked during a claim_next), it logs and moves on. The next
tick will retry.

Public surface:

  - `Watchdog(store, interval_s=30, stuck_threshold_s=600)`
  - `watchdog.start()` / `watchdog.stop()`
  - `watchdog.on(topic, async_callback)` — subscribe to a topic
  - `await watchdog.emit(topic, payload)` — internal, exposed for tests

The watchdog is intentionally not tied to any specific transport. v0.5
will register an `Alerts` callback that forwards to Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from agentsos.store import Store

log = logging.getLogger("agentsos.watchdog")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class Watchdog:
    def __init__(
        self,
        store: Store,
        interval_s: float = 30.0,
        stuck_threshold_s: float = 600.0,
    ) -> None:
        self.store = store
        self.interval_s = interval_s
        self.stuck_threshold_s = stuck_threshold_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._handlers: dict[str, list[EventHandler]] = {}
        self._last_tick: str | None = None
        self._tick_count = 0

    # --- subscription API ---

    def on(self, topic: str, handler: EventHandler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    async def emit(self, topic: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers.get(topic, [])):
            try:
                await handler(payload)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("handler for %s raised: %s", topic, exc)

    # --- lifecycle ---

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agentsos.watchdog")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self.interval_s + 1)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    # --- loop ---

    async def _run(self) -> None:
        log.info("watchdog started (interval=%ss, stuck=%ss)", self.interval_s, self.stuck_threshold_s)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("watchdog tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
        log.info("watchdog stopped")

    async def tick(self) -> None:
        """One pass of stuck + deadline detection. Public for tests."""
        self._tick_count += 1
        self._last_tick = datetime.now(UTC).isoformat(timespec="microseconds")
        await self._sweep_stuck()
        await self._sweep_deadlines()

    async def _sweep_stuck(self) -> None:
        running = self.store.subgoal_list(status="running")
        if not running:
            return
        now = datetime.now(UTC)
        for sg in running:
            if not sg.claimed_at:
                continue
            claimed = datetime.fromisoformat(sg.claimed_at)
            age = (now - claimed).total_seconds()
            if age >= self.stuck_threshold_s:
                self.store.subgoal_park(sg.id, error="watchdog: stuck")
                log.warning("subgoal %s parked (stuck for %.0fs)", sg.id, age)
                await self.emit(
                    "subgoal.stuck",
                    {
                        "subgoal_id": sg.id,
                        "goal_id": sg.goal_id,
                        "manifest_id": sg.manifest_id,
                        "age_s": age,
                        "attempts": sg.attempts,
                    },
                )

    async def _sweep_deadlines(self) -> None:
        goals = self.store.goal_list(status="active")
        now = datetime.now(UTC)
        for g in goals:
            if not g.deadline:
                continue
            try:
                deadline = datetime.fromisoformat(g.deadline)
            except ValueError:
                continue
            if now >= deadline:
                self.store.goal_update_status(g.id, "failed", finished=True)
                log.warning("goal %s past deadline (%s)", g.id, g.deadline)
                await self.emit(
                    "goal.deadline_missed",
                    {"goal_id": g.id, "name": g.name, "deadline": g.deadline},
                )

    # --- introspection ---

    def stats(self) -> dict[str, Any]:
        return {
            "tick_count": self._tick_count,
            "last_tick": self._last_tick,
            "interval_s": self.interval_s,
            "stuck_threshold_s": self.stuck_threshold_s,
            "running": self._task is not None and not self._task.done(),
        }