"""Daemon ↔ Telegram glue.

This module exposes the single function `attach_bridge(daemon)` that the
daemon's `extra_tasks` slot calls. It is the *only* place where the
Telegram optional dependency is touched; everywhere else uses the
already-imported `agentsos.telegram` surface.

Usage from a CLI:

    from agentsos.telegram.bridge import attach_bridge
    cfg.extra_tasks.append(attach_bridge(token, chat_id))

If the optional `python-telegram-bot` package is missing, or the env
vars are unset, `attach_bridge` returns a no-op coroutine so the
daemon still runs in air-gapped environments without Telegram.

The bridge:
  - Subscribes the notifier to the daemon's watchdog + cost-guard.
  - Starts a long-poll `TelegramBot` (answers /live, /status, /log).
  - Sends a "daemon back online" ping every time it starts so the
    operator gets a heartbeat proof at boot.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("agentsos.telegram.bridge")


def _read_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    return val


def attach_bridge(
    token: str | None = None,
    chat_id: str | None = None,
) -> Callable[[Any], Awaitable[None]]:
    """Return an async task factory for `DaemonConfig.extra_tasks`.

    Reads `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from env if not
    passed explicitly. Returns a no-op factory if either is missing
    or the `python-telegram-bot` package is not installed.
    """
    tok = (token or _read_env("TELEGRAM_BOT_TOKEN")).strip()
    cid = (chat_id or _read_env("TELEGRAM_CHAT_ID")).strip()

    async def _factory(daemon: Any) -> None:
        if not tok or not cid:
            log.info("telegram bridge disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")
            return

        try:
            # Lazy import so the daemon can boot without the
            # optional `python-telegram-bot` dep installed.
            from agentsos.telegram.bot import (
                TelegramNotifier,
                TelegramBot,
                attach_to_daemon,
            )
        except Exception as exc:  # pragma: no cover - import failure
            log.warning("telegram bridge disabled: %s", exc)
            return

        notifier = TelegramNotifier(chat_id=cid)
        bot = TelegramBot(
            token=tok,
            chat_id=cid,
            snapshot_fn=daemon.snapshot,
            notifier=notifier,
        )
        attach_to_daemon(daemon, notifier)

        try:
            await bot.run_forever()
        except asyncio.CancelledError:
            await bot.stop()
            raise
        except Exception as exc:  # pragma: no cover - network failure
            log.warning("telegram bridge died: %s", exc)

    _factory.__name__ = "telegram_bridge"
    return _factory
