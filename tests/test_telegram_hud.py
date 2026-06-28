"""Tests for the JARVIS HUD formatter (pure, no network).

These tests verify that the formatter produces readable, parseable
Telegram-friendly output. The Telegram client renders our block as
monospace; newlines and box-drawing must survive intact.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentsos.telegram.hud import (
    HELP_TEXT,
    CostRow,
    GoalRow,
    render_alert,
    render_cost,
    render_goals,
    render_help,
    render_live,
    render_status,
)


def _snap(**over: Any) -> dict[str, Any]:
    base = {
        "started_at": "2026-06-28T12:00:00+00:00",
        "uptime_s": 3725,
        "state_dir": "/tmp/state",
        "watchdog": {"running": True, "ticks": 124, "interval_s": 30.0},
        "cost_guard": {
            "daily_ceiling_usd": 5.0,
            "cost_today_usd": 0.1234,
            "goal": {
                "id": "g1",
                "name": "grow-org",
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
    base.update(over)
    return base


def test_render_live_has_jarvis_branding() -> None:
    text = render_live(_snap())
    assert "JARVIS" in text
    assert "LIVE FEED" in text
    assert "▰" in text or "▱" in text  # progress bar present
    assert "UPTIME" in text
    assert "01h 02m 05s" in text  # 3725s = 1h 2m 5s


def test_render_live_omits_goal_line_when_none() -> None:
    snap = _snap()
    snap["cost_guard"]["goal"] = None
    text = render_live(snap)
    assert "JARVIS" in text
    # No goal block line
    assert "grow-org" not in text


def test_render_status_keys_match_snapshot() -> None:
    snap = _snap()
    text = render_status(snap)
    assert "FULL STATUS" in text
    assert "active_goals" in text
    assert "subgoals_running" in text
    assert "interval_s" in text
    assert "$" in text  # cost block


def test_render_goals_empty_list() -> None:
    text = render_goals(rows=[], total_spent_usd=0.0)
    assert "(no goals yet" in text
    assert "GOALS" in text


def test_render_goals_lists_each_goal() -> None:
    rows = [
        GoalRow("g1", "grow-org", "active", 2.0, 0.4, 5, 2),
        GoalRow("g2", "ship-product", "paused", 5.0, 0.0, 0, 0),
    ]
    text = render_goals(rows, total_spent_usd=0.4)
    assert "[ACTIVE" in text and "grow-org" in text
    assert "[PAUSED" in text and "ship-product" in text
    assert "$0.4000" in text


def test_render_cost_lists_each_row() -> None:
    rows = [
        CostRow("2026-06-28T12:00:00Z", "run123abc", "worker-1", 100, 50, 0.001234),
        CostRow("2026-06-28T12:00:01Z", "run456def", "worker-1", 200, 80, 0.002345),
    ]
    text = render_cost(rows, total_usd=0.003579, ceiling_usd=5.0)
    assert "COST LEDGER" in text
    assert "run123abc" in text
    assert "$0.001234" in text


def test_render_alert_includes_topic_and_payload() -> None:
    text = render_alert("cost.ceiling_breached", {"scope": "daily", "ceiling": 5.0, "spent": 5.01})
    assert "ALERT" in text
    assert "cost.ceiling_breached" in text
    assert "5.0" in text
    assert "5.01" in text


def test_render_help_lists_all_commands() -> None:
    text = render_help()
    assert "/live" in text
    assert "/status" in text
    assert "/goals" in text
    assert "/cost" in text
    assert "/help" in text
    assert "/shutdown" in text


def test_hud_outputs_are_telegram_safe() -> None:
    """Telegram MarkdownV2 requires us to escape these chars: _*[]()~`>#+-=|{}.!

    We don't enable MarkdownV2 in send_message; we use plain monospace
    blocks. So we just verify length + line widths.
    """
    text = render_live(_snap())
    # Telegram max message length is 4096 chars
    assert len(text) < 4096
    # Every line is short enough to not wrap badly on a phone (~46 chars wide)
    for line in text.splitlines():
        assert len(line) <= 60, f"line too long: {line!r}"


def test_help_text_constant_lists_all_commands() -> None:
    for cmd in ("/live", "/status", "/goals", "/cost", "/add", "/pause", "/resume", "/cancel", "/shutdown"):
        assert cmd in HELP_TEXT