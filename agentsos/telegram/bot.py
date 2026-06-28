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
    _app: Any = field(default=None, init=False, repr=False)
    _poll_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _live_message_id: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def _build(self) -> None:
        _, Application, CommandHandler, ContextTypes = _import_ptb()

        async def cmd_live(update: Any, context: Any) -> None:
            snap = self.snapshot_fn()
            text = render_live(snap)
            await update.effective_message.reply_text(f"```\n{text}\n```", parse_mode="MarkdownV2")

        async def cmd_status(update: Any, context: Any) -> None:
            text = render_status(self.snapshot_fn())
            await update.effective_message.reply_text(f"```\n{text}\n```", parse_mode="MarkdownV2")

        async def cmd_help(update: Any, context: Any) -> None:
            text = render_help()
            await update.effective_message.reply_text(f"```\n{text}\n```", parse_mode="MarkdownV2")

        async def cmd_dispatch(update: Any, context: Any) -> None:
            if self.on_command is None:
                await update.effective_message.reply_text("(no command handler wired)")
                return
            cmd = update.effective_message.text.split(maxsplit=1)[0].lstrip("/").split("@")[0]
            args = (update.effective_message.text.split(maxsplit=1)[1].split()
                    if " " in update.effective_message.text else [])
            try:
                reply = await self.on_command(cmd, args)
            except Exception as exc:
                reply = f"⚠️ error: {exc}"
            await update.effective_message.reply_text(f"```\n{reply}\n```", parse_mode="MarkdownV2")

        self._app = Application.builder().token(self.token).build()
        self._app.add_handler(CommandHandler("live", cmd_live))
        self._app.add_handler(CommandHandler("status", cmd_status))
        self._app.add_handler(CommandHandler("help", cmd_help))
        for cmd in ("goals", "cost", "add", "pause", "resume", "cancel", "shutdown"):
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