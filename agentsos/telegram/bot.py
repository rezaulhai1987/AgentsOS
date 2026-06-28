"""Telegram bridge — bi-directional operator surface (calm enterprise HUD / TAO).

This module is split into two halves so we can unit-test the parts
that don't need a network:

  - `TelegramNotifier` (synchronous-ish API; just calls a wrapped
    coroutine). The daemon subscribes watchdog + cost-guard events
    into this notifier's `dispatch(topic, payload)`. The notifier
    throttles, formats, and pushes to the operator's chat.

  - `TelegramBot` (the actual network client). Holds the bot, runs
    handlers, exposes `run_forever()` and `stop()`. Heavy lifting is
    delegated to `python-telegram-bot` (optional dependency).

The bot is wired into the daemon via `attach_to_daemon(daemon,
notifier, bot)`, which registers the notifier as an event subscriber
on `Watchdog` and `CostGuard`.

We import `python-telegram-bot` lazily so the test suite (and a
daemon running without Telegram) doesn't require the optional
dependency. If the dependency is missing, the bot raises a clear
`TelegramUnavailableError` on construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentsos.telegram.hud import (
    HELP_TEXT,
    render_alert,
    render_cost,
    render_goals,
    render_help,
    render_live,
    render_status,
)

log = logging.getLogger("agentsos.telegram")

# Topics we forward from the daemon to the operator's Telegram chat.
ALERT_TOPICS = frozenset(
    {
        "subgoal.stuck",
        "goal.deadline_missed",
        "cost.ceiling_breached",
    }
)


class TelegramUnavailableError(RuntimeError):
    pass


# ---------------------------------------------------------------------
# Live-loop job registry. v0.3.9: each /live invocation gets a stable
# job_id, edits the same message every tick (rate-limited), and exits
# when cancelled by /stop, when the daemon pauses, or when the loop
# task is cancelled by the registry. The registry is the central
# truth — there can be at most `max_jobs_per_chat` concurrent loops
# per chat_id.
# ---------------------------------------------------------------------


@dataclass
class LiveJob:
    job_id: str
    chat_id: str
    message_id: int | None = None
    last_text: str = ""
    started_at: float = field(default_factory=time.monotonic)
    last_edit_at: float = 0.0
    ticks: int = 0
    task: asyncio.Task[None] | None = None

    def too_fast(self, now: float, min_interval_s: float) -> bool:
        return (now - self.last_edit_at) < min_interval_s

    def text_changed(self, new: str) -> bool:
        return new != self.last_text


class LiveJobRegistry:
    """Track /live auto-refresh loops. One job per chat_id by default."""

    def __init__(self, max_jobs_per_chat: int = 1) -> None:
        self._jobs: dict[str, LiveJob] = {}
        self.max_jobs_per_chat = max_jobs_per_chat

    def get(self, chat_id: str) -> LiveJob | None:
        return self._jobs.get(chat_id)

    def active(self) -> list[LiveJob]:
        return [j for j in self._jobs.values() if j.task is not None and not j.task.done()]

    def start(
        self,
        chat_id: str,
        message_id: int | None = None,
        job_id: str | None = None,
    ) -> LiveJob:
        # Replace any existing job for this chat (last /live wins).
        old = self._jobs.get(chat_id)
        if old is not None and old.task is not None and not old.task.done():
            old.task.cancel()
        job = LiveJob(
            job_id=job_id or f"live-{chat_id}-{int(time.time() * 1000)}",
            chat_id=chat_id,
            message_id=message_id,
        )
        self._jobs[chat_id] = job
        return job

    def stop(self, chat_id: str) -> bool:
        job = self._jobs.pop(chat_id, None)
        if job is None or job.task is None:
            return False
        if not job.task.done():
            job.task.cancel()
        return True

    def stop_all(self) -> int:
        n = 0
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()
                n += 1
        self._jobs.clear()
        return n


# --- notifier (no network; tests this without PTB) ------------------

@dataclass
class AlertRecord:
    ts: float
    topic: str
    payload: dict[str, Any]


@dataclass
class TelegramNotifier:
    """Forwards daemon events to Telegram with simple throttling.

    The notifier does NOT call Telegram directly — it puts alerts on
    an asyncio queue and a single sender task drains it. This makes
    the hot path (watchdog tick) non-blocking and lets us throttle
    spam (e.g. cost-guard firing every step).

    `send_text` is the only method that actually touches the bot.
    Tests can monkey-patch `send_text` to assert what gets sent.
    """

    chat_id: str
    min_interval_s: float = 1.0  # throttle between alerts
    queue_max: int = 256
    _queue: asyncio.Queue[AlertRecord] | None = field(default=None, init=False, repr=False)
    _last_sent: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _stats: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    send_text: Callable[[str, str], Awaitable[None]] | None = field(default=None, repr=False)
    _sender_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.queue_max)

    async def dispatch(self, topic: str, payload: dict[str, Any]) -> None:
        """Called by watchdog/cost-guard subscribers."""
        if topic not in ALERT_TOPICS:
            return  # heartbeat etc. don't go to the operator
        last = self._last_sent.get(topic, 0.0)
        if time.monotonic() - last < self.min_interval_s:
            self._stats.setdefault(f"{topic}_throttled", 0)
            self._stats[f"{topic}_throttled"] += 1
            return
        record = AlertRecord(ts=time.time(), topic=topic, payload=payload)
        try:
            self._queue.put_nowait(record)  # type: ignore[union-attr]
        except asyncio.QueueFull:
            self._stats.setdefault("queue_full", 0)
            self._stats["queue_full"] += 1

    async def _sender_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                return
            if self.send_text is None:
                continue
            text = render_alert(record.topic, record.payload)
            try:
                await self.send_text(self.chat_id, text)
            except Exception as exc:  # pragma: no cover - network
                log.warning("send_text failed: %s", exc)
                self._stats.setdefault("send_failed", 0)
                self._stats["send_failed"] += 1
            else:
                self._last_sent[record.topic] = time.monotonic()
                self._stats.setdefault(f"{record.topic}_sent", 0)
                self._stats[f"{record.topic}_sent"] += 1

    async def start(self) -> None:
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender_loop(), name="agentsos.tg.sender")

    async def stop(self) -> None:
        if self._sender_task is not None and not self._sender_task.done():
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sender_task = None

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    @staticmethod
    def dump_log(path: Path) -> list[dict[str, Any]]:
        """Read the JSONL alert log if it exists. v0.5 will persist alerts."""
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


# --- bot (network layer) ---------------------------------------------

def _import_ptb() -> tuple[Any, Any]:
    try:
        from telegram import Update  # type: ignore[import-not-found]
        from telegram.ext import (  # type: ignore[import-not-found]
            Application,
            CommandHandler,
            ContextTypes,
        )
    except ImportError as exc:
        raise TelegramUnavailableError(
            "python-telegram-bot is not installed; `uv pip install 'python-telegram-bot>=21'`"
        ) from exc
    return (Update, Application, CommandHandler, ContextTypes)


@dataclass
class TelegramBot:
    """Thin wrapper around python-telegram-bot Application.

    On construction it doesn't connect; `run()` (called from a daemon
    task) opens the long-poll. `stop()` cancels the run.

    Commands implemented:
      /live, /status, /goals, /cost, /add, /pause, /resume, /cancel,
      /shutdown, /help.

    The bot defers all data lookups to `Daemon.snapshot()` and the
    store via the callable hooks. That keeps the bot layer stateless
    and easy to test.
    """

    token: str
    chat_id: str
    snapshot_fn: Callable[[], dict[str, Any]]
    on_command: Callable[[str, list[str]], Awaitable[str]] | None = None
    notifier: TelegramNotifier | None = None
    live_interval_s: float = 30.0  # v0.3.9 /live auto-refresh tick
    live_min_edit_s: float = 5.0   # throttle Telegram editMessageText calls
    # v0.3.10: optional goal-parser dispatcher. If set, /goal is routed
    # here BEFORE on_command so quoted titles survive intact.
    goal_runner: Callable[[str], str] | None = None
    # v0.3.11: AccessGuard instance. When set, every command is
    # checked against allowlist + PIN + TOTP + rate-limit BEFORE any
    # handler runs. None means "no guard" (dev / tests only).
    guard: Any = None
    _app: Any = field(default=None, init=False, repr=False)
    _poll_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _live_message_id: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    live_registry: LiveJobRegistry = field(
        default_factory=LiveJobRegistry, init=False, repr=False
    )

    async def _run_live_loop(self, chat_id: str, message_id: int) -> None:
        """v0.3.9: edit a single message every live_interval_s until cancelled.

        Honours the pause_event if the daemon wired one (via the snapshot's
        `paused` field); silently sleeps on rate limit; never raises.
        """
        job = self.live_registry.get(chat_id)
        if job is None:
            return
        job.message_id = message_id
        try:
            while True:
                # Honour daemon pause: snapshot it and skip edits while paused.
                try:
                    snap = self.snapshot_fn()
                except Exception:  # pragma: no cover  (defensive)
                    snap = {}
                paused = bool(snap.get("paused", False)) if isinstance(snap, dict) else False
                now = time.monotonic()
                if not paused and (job.too_fast(now, self.live_min_edit_s) or job.text_changed(render_live(snap)) is False):
                    # Skip: too fast AND nothing changed.
                    if job.too_fast(now, self.live_min_edit_s) and not job.text_changed(render_live(snap)):
                        await asyncio.sleep(self.live_interval_s)
                        continue
                if not paused:
                    text = render_live(snap)
                    if job.text_changed(text) or job.last_edit_at == 0.0:
                        try:
                            await self._app.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=f"```\n{text}\n```",
                                parse_mode="MarkdownV2",
                            )
                            job.last_text = text
                            job.last_edit_at = time.monotonic()
                            job.ticks += 1
                        except Exception as exc:  # pragma: no cover  (network)
                            log.warning("live edit failed: %s", exc)
                            await asyncio.sleep(self.live_interval_s)
                            continue
                await asyncio.sleep(self.live_interval_s)
        except asyncio.CancelledError:
            log.info("live loop cancelled (chat=%s job=%s)", chat_id, job.job_id)
            raise

    def _build(self) -> None:
        _, Application, CommandHandler, ContextTypes = _import_ptb()

        async def cmd_live(update: Any, context: Any) -> None:
            chat_id = str(update.effective_chat.id)
            snap = self.snapshot_fn()
            text = render_live(snap)
            sent = await update.effective_message.reply_text(
                f"```\n{text}\n```", parse_mode="MarkdownV2"
            )
            # Start (or replace) the auto-refresh loop for this chat.
            job = self.live_registry.start(chat_id=chat_id, message_id=sent.message_id)
            job.last_text = text
            job.last_edit_at = time.monotonic()
            job.ticks = 1
            job.task = asyncio.create_task(
                self._run_live_loop(chat_id, sent.message_id),
                name=f"live-{chat_id}",
            )
            await update.effective_message.reply_text(
                f"```\n/live auto-refresh every {self.live_interval_s:.0f}s — /live_stop to end\n```",
                parse_mode="MarkdownV2",
            )

        async def cmd_live_stop(update: Any, context: Any) -> None:
            chat_id = str(update.effective_chat.id)
            stopped = self.live_registry.stop(chat_id)
            await update.effective_message.reply_text(
                f"```\n{'live stopped' if stopped else 'no live loop running'}\n```",
                parse_mode="MarkdownV2",
            )

        async def cmd_status(update: Any, context: Any) -> None:
            text = render_status(self.snapshot_fn())
            await update.effective_message.reply_text(f"```\n{text}\n```", parse_mode="MarkdownV2")

        async def cmd_help(update: Any, context: Any) -> None:
            text = render_help()
            await update.effective_message.reply_text(f"```\n{text}\n```", parse_mode="MarkdownV2")

        async def cmd_dispatch(update: Any, context: Any) -> None:
            raw = update.effective_message.text
            head = raw.split(maxsplit=1)[0].lstrip("/").split("@")[0]
            cmd = head
            rest = raw.split(maxsplit=1)[1] if " " in raw else ""
            chat_id = int(update.effective_chat.id)

            # v0.3.11: Security guard (allowlist + PIN + TOTP + rate).
            if self.guard is not None:
                verdict = self.guard.check(chat_id, cmd, first_message_text=raw)
                if not verdict.accepted:
                    if verdict.reply:
                        await update.effective_message.reply_text(verdict.reply)
                    return  # silent drop when reply is None

            # /goal uses shlex-aware parser; route through goal_runner
            # so titles like "Ship v0.4 — fast" survive intact.
            if cmd == "goal" and self.goal_runner is not None:
                try:
                    reply = self.goal_runner(rest)
                except Exception as exc:
                    reply = f"⚠️ goal error: {exc}"
                await update.effective_message.reply_text(
                    f"```\n{reply}\n```", parse_mode="MarkdownV2"
                )
                return
            if self.on_command is None:
                await update.effective_message.reply_text("(no command handler wired)")
                return
            args = rest.split() if rest else []
            try:
                reply = await self.on_command(cmd, args)
            except Exception as exc:
                reply = f"⚠️ error: {exc}"
            # /pause and /shutdown also cancel live loops.
            if cmd in ("pause", "cancel", "shutdown", "stop"):
                self.live_registry.stop(str(update.effective_chat.id))
            await update.effective_message.reply_text(f"```\n{reply}\n```", parse_mode="MarkdownV2")

        self._app = Application.builder().token(self.token).build()
        self._app.add_handler(CommandHandler("live", cmd_live))
        self._app.add_handler(CommandHandler("live_stop", cmd_live_stop))
        self._app.add_handler(CommandHandler("status", cmd_status))
        self._app.add_handler(CommandHandler("help", cmd_help))
        for cmd in ("goals", "cost", "add", "goal", "pause", "resume",
                    "cancel", "shutdown", "auth"):
            self._app.add_handler(CommandHandler(cmd, cmd_dispatch))

        # Wire the notifier (if any) to send_text.
        if self.notifier is not None:
            async def send(chat_id: str, text: str) -> None:
                await self._app.bot.send_message(chat_id=chat_id, text=f"```\n{text}\n```",
                                                 parse_mode="MarkdownV2")
            self.notifier.send_text = send

    async def run(self) -> None:
        if self._app is None:
            self._build()
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram bot polling started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        # Cancel any in-flight /live auto-refresh loops first.
        self.live_registry.stop_all()
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
        except Exception:  # pragma: no cover
            pass
        try:
            await self._app.stop()
        except Exception:  # pragma: no cover
            pass
        try:
            await self._app.shutdown()
        except Exception:  # pragma: no cover
            pass
        self._app = None
        log.info("telegram bot stopped")

    async def send_alert(self, topic: str, payload: dict[str, Any]) -> None:
        """Push a single alert (used by the notifier at runtime)."""
        if self._app is None:
            return
        text = render_alert(topic, payload)
        await self._app.bot.send_message(
            chat_id=self.chat_id, text=f"```\n{text}\n```", parse_mode="MarkdownV2"
        )


# --- integration -----------------------------------------------------

def attach_to_daemon(daemon: Any, notifier: TelegramNotifier) -> None:
    """Subscribe the notifier to the daemon's watchdog + cost guard."""
    for topic in ALERT_TOPICS:
        daemon.watchdog.on(topic, lambda p, t=topic: notifier.dispatch(t, p))
        daemon.cost_guard.on(topic, lambda p, t=topic: notifier.dispatch(t, p))


def _alert_summary(topics: tuple[str, ...] = tuple(ALERT_TOPICS)) -> str:
    return f"forwarding topics: {', '.join(sorted(topics))}"


# --- CLI plumbing ----------------------------------------------------

def cli_run(token: str, chat_id: str, snapshot_fn: Callable[[], dict[str, Any]],
            on_command: Callable[[str, list[str]], Awaitable[str]] | None = None) -> "TelegramBot":
    """Construct a bot with the notifier already wired. Used by `agents telegram run`."""
    notifier = TelegramNotifier(chat_id=chat_id)
    bot = TelegramBot(token=token, chat_id=chat_id, snapshot_fn=snapshot_fn,
                      on_command=on_command, notifier=notifier)
    return bot


async def cli_smoke(token: str, chat_id: str, snapshot_fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Send a one-shot `/live`-style card without starting the long-poll.

    Used by `agents telegram smoke` to verify the bot token + chat_id
    + formatting are all valid before the operator leaves their desk.
    """
    from telegram import Bot  # type: ignore[import-not-found]
    bot = Bot(token=token)
    text = render_live(snapshot_fn())
    sent = await bot.send_message(chat_id=chat_id, text=f"```\n{text}\n```",
                                  parse_mode="MarkdownV2")
    return {
        "message_id": sent.message_id,
        "chat_id": sent.chat_id,
        "sent_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "topic": "smoke",
    }