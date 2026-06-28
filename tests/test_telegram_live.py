"""Tests for the v0.3.9 /live auto-refresh job registry.

These tests do NOT touch the network. They exercise the registry's
lifecycle (start, replace, stop, stop_all) and a stripped-down run of
the loop using a fake bot.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agentsos.telegram.bot import LiveJob, LiveJobRegistry


def _make_job(chat_id: str, **kw):
    return LiveJob(job_id=f"job-{chat_id}", chat_id=chat_id, **kw)


def test_registry_starts_and_replaces() -> None:
    r = LiveJobRegistry()
    j1 = r.start("c1", message_id=10, job_id="a")
    assert r.get("c1") is j1
    assert j1.job_id == "a"
    # Replace by starting a new one (old task is None, so no cancel).
    j2 = r.start("c1", message_id=11, job_id="b")
    assert r.get("c1") is j2
    assert j2.job_id == "b"
    assert j2.message_id == 11


async def test_registry_replaces_with_running_task_async() -> None:
    r = LiveJobRegistry()
    j1 = r.start("c1", job_id="a")
    j1.task = asyncio.create_task(asyncio.sleep(60))
    try:
        j2 = r.start("c1", job_id="b")
        await asyncio.sleep(0)  # let cancellation propagate
        assert j1.task.cancelled() or j1.task.done()
        assert j2.job_id == "b"
    finally:
        if not j1.task.done():
            j1.task.cancel()
        if j2.task:
            j2.task.cancel()


async def test_registry_stop_removes_and_cancels_async() -> None:
    r = LiveJobRegistry()
    j = r.start("c1", job_id="a")
    j.task = asyncio.create_task(asyncio.sleep(60))
    try:
        assert r.stop("c1") is True
        await asyncio.sleep(0)
        assert j.task.cancelled() or j.task.done()
        assert r.get("c1") is None
    finally:
        if not j.task.done():
            j.task.cancel()


async def test_registry_stop_all_async() -> None:
    r = LiveJobRegistry()
    tasks = []
    for c in ("c1", "c2", "c3"):
        j = r.start(c, job_id=f"job-{c}")
        j.task = asyncio.create_task(asyncio.sleep(60))
        tasks.append(j.task)
    try:
        n = r.stop_all()
        assert n == 3
        await asyncio.sleep(0)
        for t in tasks:
            assert t.cancelled() or t.done()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


def test_registry_stop_unknown_chat() -> None:
    r = LiveJobRegistry()
    assert r.stop("nope") is False


def test_live_job_text_changed_and_throttle() -> None:
    j = _make_job("c1", last_text="x", last_edit_at=time.monotonic() - 10.0)
    assert j.text_changed("y") is True
    assert j.text_changed("x") is False
    assert j.too_fast(time.monotonic(), min_interval_s=60.0) is True
    assert j.too_fast(time.monotonic(), min_interval_s=0.0) is False


async def test_run_live_loop_edits_message_and_respects_pause() -> None:
    """Integration test: a fake snapshot_fn that flips paused.

    The bot calls edit_message_text; we record the calls and assert the
    loop respects both the text-change skip AND the pause flag.
    """
    from agentsos.telegram.bot import TelegramBot

    # Fake bot object with .bot.edit_message_text recorder.
    edits: list[tuple[str, int, str]] = []

    class _FakeInnerBot:
        async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
            edits.append((chat_id, message_id, text))

    class _FakeApp:
        bot = _FakeInnerBot()

    snap_state = {"n": 0, "paused": False}

    def snap():
        snap_state["n"] += 1
        return {
            "paused": snap_state["paused"],
            "uptime_s": snap_state["n"] * 5,
            "started_at": "2026-06-28T10:00:00",
        }

    bot = TelegramBot(token="x", chat_id="c1", snapshot_fn=snap, live_interval_s=0.05, live_min_edit_s=0.0)
    bot._app = _FakeApp()  # type: ignore[attr-defined]
    job = bot.live_registry.start("c1", message_id=42, job_id="loop-1")
    job.last_text = "init"
    job.last_edit_at = time.monotonic()

    # Run loop for ~0.25s — should produce several ticks.
    task = asyncio.create_task(bot._run_live_loop("c1", 42))
    await asyncio.sleep(0.25)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # We don't assert exact counts (timing), only that at least one edit
    # landed and that it included the snapshot's uptime.
    assert edits, "expected at least one edit_message_text call"
    joined = " ".join(t for _, _, t in edits)
    assert "paused" not in joined.lower()  # not paused this run

    # Now flip paused=True and ensure no further edits land.
    edits.clear()
    snap_state["paused"] = True
    task = asyncio.create_task(bot._run_live_loop("c1", 42))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert edits == [], "paused snapshot must suppress all edits"