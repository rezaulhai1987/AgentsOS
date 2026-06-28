"""Smoke-test the Telegram bridge: send a /live card to the operator's chat.

Usage:
    python scripts/telegram_smoke.py

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env (loaded from ~/.hermes/.env).
This is the one-shot operator verification: if you see the JARVIS card on
your phone, the bot token + chat_id + formatting are all valid.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentsos.telegram.hud import render_live


def _stub_snapshot() -> dict:
    """A realistic snapshot — same shape the daemon emits at runtime."""
    return {
        "started_at": "2026-06-28T12:00:00+00:00",
        "uptime_s": 3725,
        "state_dir": "state/",
        "watchdog": {"running": True, "ticks": 124, "interval_s": 30.0},
        "cost_guard": {
            "daily_ceiling_usd": 5.0,
            "cost_today_usd": 0.1234,
            "goal": {
                "id": "v0.3-bridge",
                "name": "Telegram JARVIS bridge",
                "budget": 2.0,
                "spent": 0.5,
                "remaining": 1.5,
            },
        },
        "store_health": {
            "active_goals": 1,
            "goals_total": 3,
            "subgoals_running": 2,
            "subgoals_pending": 5,
            "subgoals_succeeded": 7,
            "subgoals_failed": 1,
            "subgoals_parked": 0,
        },
    }


async def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2

    from telegram import Bot
    bot = Bot(token=token)

    # First check the bot is reachable
    me = await bot.get_me()
    print(f"bot authenticated as @{me.username} (id={me.id})")

    text = render_live(_stub_snapshot())
    print(f"sending {len(text)} chars to chat {chat_id}...")
    sent = await bot.send_message(
        chat_id=chat_id,
        text=f"```\n{text}\n```",
        parse_mode="MarkdownV2",
    )
    print(f"OK message_id={sent.message_id} chat_id={sent.chat_id} at {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
