"""Telegram command extensions — desktop-OS surface for TAO.

The base bot.py handles bare `/cmd` text. This module adds the
"Windows-OS-in-Telegram" affordances:

  - **`render_desktop(snapshot)`** — a single big card laid out like
    a window: top bar (clock/uptime/cost/queue), left pane (task
    tree), right pane (journal tail), bottom (status).

  - **`/desktop`** — send the desktop card once.

  - **Inline keyboard** under every command reply so the operator
    can tap buttons (live / tree / goals / cost / journal / files /
    plan / run / pause / resume / stop / help) instead of typing.

  - **`/add <text>`** — adds a goal through the daemon's command
    surface (same code path the operator would use from the CLI).

  - **`/run <plan_id>`** — runs a plan from the manifest library.

  - **`/files [path]`** — lists a directory.

  - **`/tree`** — full goal tree (parents + children) with status.

  - **`/plan [name]`** — view a plan's manifest + DAG.

  - **Auto-updating /live** — every 30s the bot edits its own
    last /live message in place (Telegram `edit_message_text`).
    No spam; one message, always current.

The keyboard layout is fixed (12 buttons) so it always fits on a
phone screen without scrolling.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from agentsos.telegram.hud import (
    HELP_TEXT,
    render_alert,
    render_cost,
    render_goals,
    render_help,
    render_live,
    render_status,
    _now,
    header,
    footer,
)

# --- inline keyboard -------------------------------------------------

KEYBOARD_LAYOUT: list[list[str]] = [
    ["live", "tree", "goals", "cost"],
    ["journal", "files", "plan", "log"],
    ["run", "add", "pause", "resume"],
    ["stop", "help"],
]

KEYBOARD_BUTTONS: dict[str, str] = {
    "live": "🟢 /live",
    "tree": "🌲 /tree",
    "goals": "🎯 /goals",
    "cost": "💰 /cost",
    "journal": "📓 /journal",
    "files": "📁 /files",
    "plan": "📋 /plan",
    "log": "📜 /log",
    "run": "▶️ /run",
    "add": "➕ /add",
    "pause": "⏸ /pause",
    "resume": "▶ /resume",
    "stop": "⏹ /stop",
    "help": "❓ /help",
}


def build_keyboard() -> list[list[dict[str, str]]]:
    """Return a 4x4 (mostly) inline keyboard for Telegram."""
    out: list[list[dict[str, str]]] = []
    for row in KEYBOARD_LAYOUT:
        out.append(
            [{"text": KEYBOARD_BUTTONS[k], "callback_data": f"cmd:{k}"} for k in row]
        )
    return out


# --- /desktop — the big window-OS card ------------------------------

def render_desktop(snapshot: dict[str, Any], *, journal_tail: int = 8) -> str:
    """One big card that looks like a windowed desktop.

    Layout (ASCII so it renders cleanly in monospace Telegram blocks):
      ┌─ ⟁ TAO // DESKTOP ─────────────...
      │ TOP BAR     clock | uptime | cost | queue | agents
      │ LEFT PANE   task tree (parent → children with status)
      │ RIGHT PANE  journal tail (last N events)
      │ BOTTOM      hint row + next-action suggestion
    """
    parts: list[str] = []
    parts.append(header("DESKTOP", "TAO // agentsos.live"))

    # --- top bar
    clock = _now()
    uptime_s = int(snapshot.get("uptime_s", 0))
    hours, rem = divmod(uptime_s, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime = f"{hours:02d}h {minutes:02d}m {seconds:02d}s"
    cost_today = float(snapshot.get("cost_guard", {}).get("today_usd", 0.0))
    ceiling = float(snapshot.get("cost_guard", {}).get("ceiling_usd", 5.0))
    queue = int(snapshot.get("queue_depth", 0))
    agents_alive = int(snapshot.get("agents_alive", 0))
    parts.append(f"│ TOP  ⏱ {clock}  ⏳ {uptime}  💰 ${cost_today:.2f}/${ceiling:.2f}  📥 {queue}  🤖 {agents_alive}")
    parts.append("│ ─────────────────────────────────────────────────────────")

    # --- left pane: task tree
    parts.append("│ LEFT  ░ TASK TREE")
    tree = snapshot.get("task_tree") or []
    if not tree:
        parts.append("│   (no tasks — try /add to start a goal)")
    else:
        for node in tree:
            indent = "  " * int(node.get("depth", 0))
            status = str(node.get("status", "?"))[:8]
            name = str(node.get("name", node.get("id", "?")))[:36]
            bar = _mini_bar(node.get("progress", 0.0))
            parts.append(f"│   {indent}├─ [{status:<8}] {bar} {name}")
    parts.append("│ ─────────────────────────────────────────────────────────")

    # --- right pane: journal tail
    parts.append(f"│ RIGHT ░ JOURNAL (last {journal_tail})")
    journal = snapshot.get("journal_tail", [])[-journal_tail:]
    if not journal:
        parts.append("│   (journal empty)")
    else:
        for entry in journal:
            ts = str(entry.get("ts", ""))[:19]
            topic = str(entry.get("topic", "?"))[:18]
            parts.append(f"│   {ts}  {topic}")
    parts.append("│ ─────────────────────────────────────────────────────────")

    # --- bottom: hint
    next_action = snapshot.get("next_action") or "(idle)"
    parts.append(f"│ NEXT  ▶ {str(next_action)[:60]}")
    parts.append("│")
    parts.append("│ Tap a button below  /  Type /<command>")
    parts.append(footer())
    return "\n".join(parts)


def _mini_bar(p: float, width: int = 10) -> str:
    p = max(0.0, min(1.0, float(p)))
    filled = int(p * width)
    return "▰" * filled + "▱" * (width - filled)


# --- /tree — focused task tree ---------------------------------------

def render_tree(snapshot: dict[str, Any]) -> str:
    tree = snapshot.get("task_tree") or []
    parts: list[str] = []
    parts.append(header("TASK TREE", f"{len(tree)} nodes"))
    parts.append("│")
    if not tree:
        parts.append("│  (no tasks yet — `/add <goal>` to create one)")
    else:
        for node in tree:
            indent = "  " * int(node.get("depth", 0))
            status = str(node.get("status", "?"))
            bar = _mini_bar(node.get("progress", 0.0))
            name = str(node.get("name", node.get("id", "?")))[:48]
            parts.append(f"│  {indent}├─ [{status:<10}] {bar} {name}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /journal — focused journal tail ---------------------------------

def render_journal(snapshot: dict[str, Any], n: int = 20) -> str:
    journal = snapshot.get("journal_tail", [])[-n:]
    parts: list[str] = []
    parts.append(header("JOURNAL", f"last {len(journal)} of {snapshot.get('journal_total', len(journal))}"))
    parts.append("│")
    if not journal:
        parts.append("│  (journal empty)")
    else:
        for entry in journal:
            ts = str(entry.get("ts", ""))[:19]
            topic = str(entry.get("topic", "?"))[:24]
            payload = entry.get("payload") or {}
            payload_s = json.dumps(payload, separators=(",", ":"))[:48] if payload else ""
            parts.append(f"│  {ts}  {topic:<24}  {payload_s}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /files — directory listing ---------------------------------------

def render_files(path: str | Path, base: Path) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base / p
    parts: list[str] = []
    parts.append(header("FILES", str(p)))
    parts.append("│")
    if not p.exists():
        parts.append(f"│  ✗ not found: {p}")
    elif not p.is_dir():
        size = p.stat().st_size
        parts.append(f"│  📄 {p.name}  ({size:,} bytes)")
    else:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        if not entries:
            parts.append("│  (empty directory)")
        for e in entries[:60]:
            if e.is_dir():
                parts.append(f"│  📁 {e.name}/")
            else:
                size = e.stat().st_size
                parts.append(f"│  📄 {e.name:<48} {size:>10,} B")
        if len(entries) > 60:
            parts.append(f"│  … and {len(entries) - 60} more")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /plan — view a manifest + DAG ------------------------------------

def render_plan(plan: dict[str, Any]) -> str:
    parts: list[str] = []
    name = str(plan.get("name", "?"))
    pid = str(plan.get("id", "?"))
    parts.append(header("PLAN", f"{name}  ·  {pid}"))
    parts.append("│")
    desc = str(plan.get("description", ""))
    if desc:
        for line in desc.splitlines()[:6]:
            parts.append(f"│  {line[:60]}")
        parts.append("│")
    steps = plan.get("steps", [])
    parts.append(f"│  STEPS ({len(steps)}):")
    for i, s in enumerate(steps, 1):
        sid = s.get("id", f"step-{i}")
        agent = s.get("agent", "?")
        deps = ",".join(s.get("depends_on", [])) or "—"
        parts.append(f"│  {i:>3}. [{sid:<24}] agent={agent:<12} deps={deps}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /log — daemon JSONL tail ----------------------------------------

def render_log(lines: list[str], n: int = 30) -> str:
    parts: list[str] = []
    parts.append(header("DAEMON LOG", f"last {min(n, len(lines))} lines"))
    parts.append("│")
    for ln in lines[-n:]:
        parts.append(f"│  {ln[:80]}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- updated help ----------------------------------------------------

EXTENDED_HELP = (
    "⟁ TAO COMMANDS (extended)\n"
    "────────────────────────────\n"
    "/live     Live status card (auto-updating every 30s)\n"
    "/desktop  Full windowed dashboard (top+tree+journal)\n"
    "/status   Full daemon telemetry\n"
    "/tree     Goal/task tree with progress bars\n"
    "/goals    List of all goals + progress\n"
    "/journal  Last 20 journal events\n"
    "/files    Directory listing: `/files [path]`\n"
    "/plan     View plan manifest: `/plan [name]`\n"
    "/log      Daemon JSONL tail\n"
    "/cost     Cost ledger + daily ceiling\n"
    "/add      Add a goal: `/add <text>`\n"
    "/run      Run a plan: `/run <plan_id>`\n"
    "/pause    Pause: `/pause <goal_id|all>`\n"
    "/resume   Resume: `/resume <goal_id|all>`\n"
    "/cancel   Cancel: `/cancel <goal_id|all>`\n"
    "/stop     Gracefully stop the daemon\n"
    "/help     This message\n"
    "\n"
    "Inline buttons below every reply — tap, don't type.\n"
    "Alerts auto-push on stuck subgoals, missed deadlines, cost ceilings."
)


def render_help_extended() -> str:
    parts: list[str] = []
    parts.append(header("HELP", "TAO operator reference"))
    parts.append("│")
    for line in EXTENDED_HELP.splitlines():
        parts.append(f"│  {line}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)