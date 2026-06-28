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
    registry_factory: Callable[[], Any] | None = None,
) -> Callable[[Any], Awaitable[None]]:
    """Return an async task factory for `DaemonConfig.extra_tasks`.

    Reads `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from env if not
    passed explicitly. Returns a no-op factory if either is missing
    or the `python-telegram-bot` package is not installed.

    `registry_factory` (optional) — callable that returns a fresh
    `agentsos.work_registry.Registry` each time it's called. If
    provided, `/goal` (and the CLI `agents goal ...`) will be wired
    up. The factory pattern lets tests inject a temp registry.
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

        # v0.3.8: wire on_command so /pause /resume /stop reach the
        # daemon kill-switch. The handler is intentionally tiny —
        # it just dispatches on the verb and returns a short
        # human reply. The bot re-uses the rendered snapshot on
        # state-change confirmations.
        async def _on_command(cmd: str, args: list[str]) -> str:
            reason = " ".join(args).strip() or "telegram"
            if cmd == "pause":
                await daemon.pause(reason=reason)
                return f"⏸ Paused ({reason})."
            if cmd == "resume":
                await daemon.resume(reason=reason)
                return f"▶ Resumed ({reason})."
            if cmd in ("cancel", "shutdown", "stop"):
                # Schedule shutdown so we can reply first, then stop.
                asyncio.create_task(daemon.shutdown(reason=reason))
                return "🛑 Shutdown scheduled."
            return f"(unhandled: {cmd})"

        # v0.3.10: optional /goal handler. If a registry_factory was
        # provided, build a goal_runner that maps a slash-command's
        # tail text to a friendly reply using the shared parser.
        goal_runner: Callable[[str], str] | None = None
        if registry_factory is not None:
            try:
                from agentsos.goal_parser import run_goal_command
            except Exception as exc:  # pragma: no cover
                log.warning("goal_parser import failed: %s", exc)
            else:
                def _goal_runner(text: str) -> str:
                    return run_goal_command(registry_factory(), text)

                goal_runner = _goal_runner

        # v0.3.11: AccessGuard — hard-codes the allowlist to the
        # primary operator chat unless RHAIONOS_ALLOWED_CHAT_IDS
        # widens it. PIN + TOTP loaded from env. Audit log lands in
        # <state_dir>/security.log.jsonl.
        from agentsos.telegram.guard import AccessGuard, build_default_audit
        guard = AccessGuard.from_env(audit=build_default_audit(state_dir=daemon.state_dir))

        bot = TelegramBot(
            token=tok,
            chat_id=cid,
            snapshot_fn=daemon.snapshot,
            notifier=notifier,
            on_command=_on_command,
            goal_runner=goal_runner,
            guard=guard,
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
