"""TAO-style dashboard formatter (pure, no network).

Lives in `agentsos/telegram/hud.py` (subpackage so we can ship a thin
bot layer in `bot.py` separately). The formatter is intentionally
side-effect-free — given a snapshot dict it returns a string. That
makes it trivial to unit-test.

Design language (calm enterprise HUD / TAO):

  - Cyan `#1FB6FF` brackets + amber `#FFB300` highlights (we use ASCII
    glyphs for terminal-first readability; the Telegram client will
    render them as monospace blocks).
  - Box-drawing rules for frames.
  - Wide progress bars (▰▰▱▱▱ style) so they read on a phone.
  - Timestamps in ISO 8601 UTC so the operator's logs are unambiguous.

Every formatter takes a snapshot from `Daemon.snapshot()` plus optional
context (a single goal, a cost row, an alert payload). The bot layer
plugs in actual snapshot gathering; the formatter never touches the
store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


# --- helpers ----------------------------------------------------------

def _bar(filled: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    ratio = max(0.0, min(1.0, filled / total))
    n_filled = int(round(ratio * width))
    n_empty = width - n_filled
    return "▰" * n_filled + "▱" * n_empty


def _pct(filled: int, total: int) -> str:
    if total <= 0:
        return "  0%"
    return f"{int(round(100 * filled / total)):3d}%"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


# --- header / footer --------------------------------------------------

def header(title: str, subtitle: str = "") -> str:
    bar = "─" * max(8, 36 - len(title))
    head = f"┌─ ⟁ TAO // {title} {bar}"
    if subtitle:
        head += f"\n│  {subtitle}"
    head += f"\n│  ⏱ {_now()}"
    return head


def footer() -> str:
    return "└─ ⟁ END OF TRANSMISSION ──"


# --- /live — live progress card ---------------------------------------

def render_live(snapshot: dict[str, Any]) -> str:
    """Single-message live status card.

    Designed to be edited in place every N seconds by the bot, so
    Telegram shows the user a constantly-updating card without
    flooding their chat.
    """
    health = snapshot.get("store_health", {})
    cost = snapshot.get("cost_guard", {})
    uptime = int(snapshot.get("uptime_s", 0))
    uptime_str = f"{uptime // 3600:02d}h {(uptime % 3600) // 60:02d}m {uptime % 60:02d}s"

    active = health.get("active_goals", 0)
    total = health.get("goals_total", 0)
    parked = health.get("subgoals_parked", 0)
    succeeded = health.get("subgoals_succeeded", 0)
    failed = health.get("subgoals_failed", 0)
    running = health.get("subgoals_running", 0)
    pending = health.get("subgoals_pending", 0)

    cost_today = cost.get("cost_today_usd", 0.0)
    cost_ceiling = cost.get("daily_ceiling_usd", 1.0)

    parts: list[str] = []
    parts.append(header("LIVE FEED", "AgentsOS daemon — always on"))
    parts.append("│")
    parts.append(f"│  ⏱ UPTIME       {uptime_str}")
    parts.append(f"│  🟢 DAEMON       {'ONLINE' if snapshot.get('watchdog', {}).get('running') else 'OFFLINE'}")
    parts.append("│")
    parts.append(f"│  ▸ GOALS        {_bar(active, max(active + parked, 1))} {active} active / {total} total")
    parts.append(f"│  ▸ SUBGOALS     {_bar(succeeded, succeeded + failed + running + pending)}")
    parts.append(
        f"│    ├ running    {running}"
    )
    parts.append(
        f"│    ├ pending    {pending}"
    )
    parts.append(
        f"│    ├ succeeded  {succeeded}"
    )
    parts.append(
        f"│    ├ failed     {failed}"
    )
    parts.append(
        f"│    └ parked     {parked}"
    )
    parts.append("│")
    parts.append(
        f"│  ▸ COST         {_bar(int(round(cost_today * 100)), int(round(cost_ceiling * 100)))} "
        f"${cost_today:.4f} / ${cost_ceiling:.2f} ({_pct(int(round(cost_today * 100)), int(round(cost_ceiling * 100)))})"
    )
    if cost.get("goal"):
        g = cost["goal"]
        parts.append(
            f"│    └ {g.get('name', '?')[:32]:<32} "
            f"${g.get('spent', 0):.4f} / ${g.get('budget', 0):.2f}"
        )
    parts.append("│")
    parts.append("│  /goals • /cost • /status • /help")
    parts.append(footer())
    return "\n".join(parts)


# --- /status — full snapshot -----------------------------------------

def render_status(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(header("FULL STATUS", "AgentsOS daemon telemetry"))
    parts.append("│")
    health = snapshot.get("store_health", {})
    for k, v in health.items():
        parts.append(f"│  • {k:<24} {v}")
    parts.append("│")
    parts.append("│  ▸ WATCHDOG")
    wd = snapshot.get("watchdog", {})
    for k, v in wd.items():
        parts.append(f"│    • {k:<22} {v}")
    parts.append("│")
    parts.append("│  ▸ COST GUARD")
    cg = snapshot.get("cost_guard", {})
    parts.append(f"│    • today                  ${cg.get('cost_today_usd', 0):.4f}")
    parts.append(f"│    • daily_ceiling          ${cg.get('daily_ceiling_usd', 0):.2f}")
    if cg.get("goal"):
        g = cg["goal"]
        parts.append(f"│    • goal[{g.get('name', '?')[:24]}]  ${g.get('spent', 0):.4f} / ${g.get('budget', 0):.2f}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /goals — goal list ----------------------------------------------

@dataclass
class GoalRow:
    id: str
    name: str
    status: str
    cost_budget: float
    cost_spent: float
    subgoals_total: int
    subgoals_succeeded: int


def render_goals(rows: list[GoalRow], total_spent_usd: float) -> str:
    parts: list[str] = []
    parts.append(header("GOALS", f"{len(rows)} tracked — total spent ${total_spent_usd:.4f}"))
    parts.append("│")
    if not rows:
        parts.append("│  (no goals yet — add one with `agents goal add \"...\"`)")
    for r in rows:
        bar = _bar(r.subgoals_succeeded, r.subgoals_total or 1)
        pct = _pct(r.subgoals_succeeded, r.subgoals_total or 1)
        parts.append(f"│  [{r.status.upper():<8}] {r.name}")
        parts.append(
            f"│     id={r.id[:8]}  progress {bar} {pct}  "
            f"{r.subgoals_succeeded}/{r.subgoals_total or 0} subgoals"
        )
        parts.append(
            f"│     cost ${r.cost_spent:.4f} / ${r.cost_budget:.2f}"
        )
        parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /cost — cost ledger snapshot ------------------------------------

@dataclass
class CostRow:
    ts: str
    run_id: str
    worker: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


def render_cost(rows: list[CostRow], total_usd: float, ceiling_usd: float) -> str:
    parts: list[str] = []
    parts.append(header("COST LEDGER", f"${total_usd:.4f} today — ceiling ${ceiling_usd:.2f}"))
    parts.append("│")
    if not rows:
        parts.append("│  (no cost records yet)")
    for r in rows[:10]:
        parts.append(
            f"│  {r.ts}  run={r.run_id[:10]:<10}  "
            f"in={r.tokens_in:>5} out={r.tokens_out:>4}  ${r.cost_usd:.6f}"
        )
    parts.append("│")
    bar = _bar(int(round(total_usd * 100)), int(round(ceiling_usd * 100)))
    parts.append(f"│  USAGE {bar} {int(round(100 * total_usd / ceiling_usd))}%")
    parts.append(footer())
    return "\n".join(parts)


# --- alerts — pushed by watchdog + cost guard ------------------------

def render_alert(topic: str, payload: dict[str, Any]) -> str:
    """Single-message alert, pushed to the operator's chat."""
    icon = {
        "subgoal.stuck": "🟠",
        "goal.deadline_missed": "🔴",
        "cost.ceiling_breached": "💰",
        "daemon.heartbeat": "💚",
    }.get(topic, "⚠️")
    parts: list[str] = []
    parts.append(header(f"ALERT // {topic}", f"{icon} {topic}"))
    parts.append("│")
    for k, v in payload.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"│  • {k:<22} {s}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)


# --- /help ----------------------------------------------------------

HELP_TEXT = (
    "⟁ TAO COMMANDS\n"
    "────────────────────────────\n"
    "/live     Live status card (auto-updating)\n"
    "/status   Full daemon telemetry\n"
    "/goals    List of all goals + progress\n"
    "/cost     Cost ledger + daily ceiling\n"
    "/add      Add a new goal: `/add grow the org to 10k users`\n"
    "/pause    Pause goal: `/pause <goal_id|all>`\n"
    "/resume   Resume goal: `/resume <goal_id|all>`\n"
    "/cancel   Cancel goal: `/cancel <goal_id|all>`\n"
    "/shutdown Gracefully stop the daemon\n"
    "/help     This message\n"
    "\n"
    "Alerts auto-push on stuck subgoals, missed deadlines, cost ceilings."
)


def render_help() -> str:
    parts: list[str] = []
    parts.append(header("HELP", "Operator command reference"))
    parts.append("│")
    for line in HELP_TEXT.splitlines():
        parts.append(f"│  {line}")
    parts.append("│")
    parts.append(footer())
    return "\n".join(parts)