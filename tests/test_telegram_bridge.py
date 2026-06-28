"""Tests for agentsos.telegram.bridge — the daemon↔Telegram glue.

These tests do NOT exercise the network. They verify:
  - `attach_bridge` returns a no-op factory when env is unset.
  - `attach_bridge` returns a no-op factory when chat_id/token missing.
  - The returned factory accepts a daemon-shaped object and returns a
    coroutine that completes without raising when notifier/bot import
    is missing (graceful degradation).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agentsos.telegram.bridge import attach_bridge


def _daemon_stub() -> object:
    """A tiny duck-typed daemon: has `snapshot`, `watchdog.on`,
    `cost_guard.on`. We don't construct a real Daemon here because the
    bridge factory only runs when called as an extra_task; the test
    verifies wiring without bringing up the store/watchdog.
    """

    class _StubBus:
        def __init__(self) -> None:
            self.subs: list[tuple[str, object]] = []

        def on(self, topic: str, fn: object) -> None:
            self.subs.append((topic, fn))

    class _StubDaemon:
        def __init__(self) -> None:
            self.watchdog = _StubBus()
            self.cost_guard = _StubBus()

        def snapshot(self) -> dict:
            return {"uptime_s": 0.0, "watchdog": {"running": False}}

    return _StubDaemon()


def test_attach_bridge_returns_callable() -> None:
    factory = attach_bridge()
    assert callable(factory)


def test_attach_bridge_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    factory = attach_bridge()
    daemon = _daemon_stub()
    # Should return a coroutine that completes cleanly without raising
    asyncio.run(factory(daemon))


def test_attach_bridge_with_explicit_args_but_no_pkg(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the optional dep is missing, the factory degrades to a no-op
    rather than crashing the daemon."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    factory = attach_bridge(token="x" * 10, chat_id="123")
    daemon = _daemon_stub()
    asyncio.run(factory(daemon))


def test_attach_bridge_empty_token_yields_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty env vars (whitespace only) must not crash the daemon."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    factory = attach_bridge()
    daemon = _daemon_stub()
    asyncio.run(factory(daemon))
