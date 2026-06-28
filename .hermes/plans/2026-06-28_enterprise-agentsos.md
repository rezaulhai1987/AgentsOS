# Enterprise AgentsOS Implementation Plan

> **For Hermes:** Ship in 5 phase-branches, each green-tests + ruff-clean + PR.
> Target outcome: `agentsos daemon` running 24/7, operator lives in Telegram.

**Goal:** Turn AgentsOS from a single-agent runtime (v0.2d) into an unattended,
multi-agent organization that an operator monitors from Telegram and never
opens their terminal for.

**Architecture:** Layered daemon. Persistent goal store (SQLite) is the
spine. A watchdog drives a reactive orchestrator that dispatches agents
through the existing Runtime. A Telegram bridge is the operator's
console. A goal decomposer turns natural-language objectives into agent
DAGs. A self-healing layer replays failed subgoals and injects corrective
prompts.

**Tech Stack:** Python 3.11, asyncio, SQLite, pydantic, httpx,
python-telegram-bot (or aiogram), typer, rich, tokenlab. No new
heavy deps — `sqlite3` is stdlib; for the Telegram bridge we use the
official `python-telegram-bot` library (well-maintained, async-native).

---

## Branch / PR Layout

| PR | Branch (off) | Scope | Tests added |
|----|---------------|-------|-------------|
| #4 | `feat/v0.3-always-on` (off `feat/checkpoint-resume`) | Always-on OS: goal store, watchdog, cost guard, `status` | 12+ |
| #5 | `feat/v0.4-reactive-orchestrator` (off #4) | Reactive orchestrator, DAGs, DLQ, handoff | 10+ |
| #6 | `feat/v0.5-telegram-bridge` (off #5) | Telegram bridge, CLI command parser | 8+ |
| #7 | `feat/v0.6-goal-decomposer` (off #6) | Goal decomposer + replanner | 6+ |
| #8 | `feat/v0.7-self-healing` (off #7) | Self-healing + operator memory | 8+ |

Total target: 67 → ~110 tests green, ruff + format clean throughout.

---

## v0.3 — Always-On OS Layer (PR #4)

The foundation. Without this, no later layer makes sense.

### v0.3.1 — `agentsos/store.py` — SQLite-backed goal store

`Goal` row: `id, name, description, status (active|paused|done|failed), cost_budget_usd, deadline, created_at, finished_at, parent_id (for subgoals)`.

`Subgoal` row: `id, goal_id, manifest_id, input, status, attempts, last_error, output, checkpoint_path`.

`CostLedger` row: `id, run_id, agent_name, ts, tokens_in, tokens_out, cost_usd`.

API:
- `Store(path)` — opens/creates DB
- `goal_create(...)`, `goal_get(id)`, `goal_list(status=...)`, `goal_update_status(id, status)`
- `subgoal_create(...)`, `subgoal_claim_next(goal_id)` (atomic — returns the next `pending` subgoal or `None`), `subgoal_complete(id, output, checkpoint_path)`
- `cost_record(...)`, `cost_sum(goal_id=None, since=...)` → `(tokens_in, tokens_out, cost_usd)`
- `healthcheck()` → `{goals, subgoals, total_cost_today, stuck_subgoals}`

### v0.3.2 — `agentsos/watchdog.py` — heartbeat + stuck detection

`Watchdog(Store, interval_s=30, stuck_threshold_s=600)`.

- A single async task that ticks every `interval_s`:
  - Find subgoals in `running` state whose `claimed_at` is older than `stuck_threshold_s` → mark `failed` with `error="watchdog: stuck"`; emit `subgoal.stuck` event on the event bus.
  - Find goals past their `deadline` → mark `failed`; emit `goal.deadline_missed`.
- Subscribes to a list of `on_stuck`, `on_deadline` async callbacks so the Telegram bridge can pick them up later.

### v0.3.3 — `agentsos/cost_guard.py` — budget enforcement

`CostGuard(Store, daily_ceiling_usd=50.0)`.

- `async def check(goal_id) -> bool` — returns `False` if today's spend would exceed the ceiling. The runtime calls this before each step.
- On breach, raises `CostCeilingBreached` and emits `cost.ceiling_breached` event with full ledger snapshot.

### v0.3.4 — `agentsos/ui/cli/status.py` — `agents status` command

`agents status [--json] [--goal <id>]`:
- Plain text: table of all active goals with cost-so-far / budget, subgoals, last activity.
- JSON: full structured dump for machine consumers (the Telegram bridge reads this in v0.5).

### v0.3.5 — `agentsos/daemon.py` — `agents daemon` command

The entry point that everything else hangs off. For v0.3, it just:
- Opens the store
- Starts the watchdog
- Logs to JSONL: `{ts, event, payload}`
- Blocks on SIGTERM / SIGINT for clean shutdown
- v0.4 will add the orchestrator tick; v0.5 will add the Telegram bridge; v0.6 will add the decomposer; v0.7 will add the self-healing supervisor.

### v0.3 tests (12+)

`tests/test_store.py`:
- `test_goal_create_and_get`
- `test_goal_list_filters_by_status`
- `test_subgoal_claim_next_is_atomic` (use threading to race two claims)
- `test_subgoal_complete_writes_output_and_checkpoint`
- `test_cost_record_and_sum`
- `test_healthcheck_returns_counts`

`tests/test_watchdog.py`:
- `test_watchdog_marks_stuck_subgoal_as_failed`
- `test_watchdog_emits_event_on_stuck`
- `test_watchdog_marks_deadline_missed_goal`
- `test_watchdog_ignores_fresh_subgoals`

`tests/test_cost_guard.py`:
- `test_cost_guard_allows_under_ceiling`
- `test_cost_guard_denies_over_ceiling`
- `test_cost_guard_emits_event_on_breach`

`tests/test_daemon.py`:
- `test_daemon_starts_and_stops_cleanly`
- `test_daemon_writes_jsonl_heartbeat`

---

## v0.4 — Reactive Orchestrator (PR #5)

Replace the v0.2 stub `GraphRunner` with a real reactive engine.

### v0.4.1 — `agentsos/reactor.py` — topic engine

`Reactor(Store, Runtime)`.

- Topics: `subgoal.created`, `subgoal.completed`, `subgoal.failed`, `goal.completed`, `agent.handoff`, `cost.tick`, `watchdog.stuck`.
- `Reactor.subscribe(topic, handler)` — registers a coroutine.
- `Reactor.publish(topic, payload)` — fans out to all subscribers.
- Special handlers: `on_subgoal_completed` claims the next pending subgoal for the same goal. `on_subgoal_failed` increments attempts and reschedules (up to 3) before parking in DLQ.

### v0.4.2 — `agentsos/dag.py` — multi-agent DAGs

Manifest gains optional `depends_on: list[str]` (other manifest IDs).
`DAGExecutor` walks the goal's subgoals in topological order, respecting `depends_on`. Cycles → `DAGHasCycle` error at goal creation time.

### v0.4.3 — `agentsos/dlq.py` — dead-letter queue

Failed subgoals beyond retry land in `dlq` table. Operator can:
- `agents dlq list`
- `agents dlq replay <id>` (re-creates the subgoal as fresh)
- `agents dlq drop <id>` (gives up — marks the parent goal as `partial`)

### v0.4.4 — `agentsos/handoff.py` — inter-agent context

When a `manifest.handoff_target` field is set, the assistant's final answer is automatically injected as `context` into the downstream manifest's user message. Enables chains like `researcher → writer → reviewer`.

### v0.4 tests (10+)

`tests/test_reactor.py`: subscribe/publish round-trip, handler is async, multi-subscriber fan-out.
`tests/test_dag.py`: topo order, cycle detection, missing-dep failure, independent subgoals run in parallel.
`tests/test_dlq.py`: failed-after-retries lands in DLQ, replay re-creates, drop marks partial.
`tests/test_handoff.py`: upstream output becomes downstream input, missing target = no-op, multi-hop chains.

---

## v0.5 — Telegram Operator Surface (PR #6)

The user's "I just monitor in Telegram" requirement.

### v0.5.1 — `agentsos/telegram/bridge.py` — bi-directional bot

Uses `python-telegram-bot` (already async). Configured via env vars:
- `AGENTSOS_TELEGRAM_TOKEN` (bot token from @BotFather)
- `AGENTSOS_TELEGRAM_OPERATOR_ID` (single chat_id — locked to one user for safety)
- `AGENTSOS_TELEGRAM_ALLOWED_IDS` (optional comma-separated allowlist)

Commands (matched on the first word, case-insensitive):
- `status` — calls `agents status --json`, renders a Telegram-friendly summary
- `goal <name> | <description> | $<budget> | <deadline>` — creates a goal
- `goals` — lists active goals
- `pause <goal_id>` / `resume <goal_id>` / `cancel <goal_id>`
- `cost` — today's spend + per-goal breakdown
- `dlq` — list DLQ entries
- `help` — full command list

Long messages: split at 4000 chars (Telegram limit is 4096). Use markdown. Reply to the triggering message so the operator's chat stays clean.

### v0.5.2 — `agentsos/telegram/alerts.py` — push subsystem

Subscribes to the reactor's `cost.ceiling_breached`, `watchdog.stuck`, `goal.deadline_missed`, `subgoal.failed` (after retries) topics. Pushes a concise alert message to the operator's chat. Throttled: at most 1 alert per topic per goal per 5 minutes (configurable).

### v0.5.3 — `agentsos/ui/cli/goal.py` — `agents goal` subcommands

`goal add`, `goal list`, `goal show <id>`, `goal pause <id>`, `goal resume <id>`, `goal cancel <id>`. Backed entirely by the store. The Telegram bridge is a thin client over these.

### v0.5 tests (8+)

`tests/test_telegram_commands.py`: parse `goal X | Y | $1 | 2026-07-01` correctly, reject malformed, lock to operator_id, split long output.
`tests/test_telegram_alerts.py`: subscribe to topic, push formatted message, throttle repeat.
`tests/test_cli_goal.py`: `goal add` round-trip, `goal pause` flips status, `goal cancel` parks subgoals.

Live smoke: after merge, I'll run a one-off `python -c "asyncio.run(bridge.smoke())"` that creates a goal via the CLI, lets the watchdog tick once, and verifies a status message is delivered to my Telegram.

---

## v0.6 — Goal Decomposer (PR #7)

The "give it a high-level goal" capability.

### v0.6.1 — `agentsos/decomposer.py` — goal → DAG

`Decomposer(LLMClient, manifest_registry)`.

- `async def decompose(goal_description, budget_usd, deadline) -> list[SubgoalSpec]`
- Builds a planning prompt that includes the list of available manifests (names + descriptions).
- Asks the LLM to emit a JSON list of `{manifest_id, input, depends_on[]}`.
- Validates: every `manifest_id` exists; `depends_on` references valid IDs within the same plan; no cycles.
- On invalid output: one retry with the validation error in the prompt. On second failure: raise `DecompositionFailed` (operator sees in Telegram).

### v0.6.2 — `agentsos/replanner.py` — replan on stall

`Replanner(Store, Decomposer, Reactor)`.

- Subscribes to `watchdog.stuck` for subgoals with `attempts < 2`.
- Pulls the parent goal, re-decomposes with current state as context, replaces the failed subgoal with the new plan (preserves checkpoint for forensics).
- If `attempts >= 2`, escalate to operator via `agentsos.telegram.alerts`.

### v0.6 tests (6+)

`tests/test_decomposer.py`: prompt contains manifest list, parses valid JSON, rejects unknown manifest, retry on bad output, gives up after 2.
`tests/test_replanner.py`: replan replaces subgoal, preserves checkpoint, escalates after threshold.

Live smoke: against local Ollama, decompose "post a daily summary to a log file" into `reader → writer → reviewer` and execute the plan to completion.

---

## v0.7 — Self-Healing + Operator Memory (PR #8)

The last 5% that makes the OS "set and forget."

### v0.7.1 — `agentsos/supervisor.py` — corrective-prompt loop detection

Watches `Runtime.run` for a 3-of-5 consecutive same-tool-call pattern (e.g. calling `read_file` on the same path 5x). Injects a system-level corrective prompt: "You've been retrying the same call. Try a different approach or report the blocker to the operator." The next completion must be different or the run is parked with `error="loop_detected"`.

### v0.7.2 — `agentsos/runtime.py` patch — auto-resume on transient failure

If the underlying LLM call raises `httpx.TransportError` or `asyncio.TimeoutError`, retry with exponential backoff (3 attempts, 2/4/8s). If still failing, raise; the reactor's `subgoal.failed` handler takes over and schedules a retry (which re-uses the v0.2d checkpoint resume).

### v0.7.3 — `agentsos/telegram/memory.py` — operator thread persistence

Telegram chat state (last seen command, in-flight goal add, etc.) survives a daemon restart. Backed by a `chat_state` table. Also caches the last 50 messages per chat for context (helps with multi-step "create a goal... now pause it... now show me status" flows).

### v0.7 tests (8+)

`tests/test_supervisor.py`: detect 3-of-5 same-tool loop, inject corrective prompt, park on continued loop.
`tests/test_runtime_retry.py`: transport error retries 3x, then raises; timeout retries; success on attempt 2.
`tests/test_telegram_memory.py`: state survives restart, multi-step command context, max-50-message cap.

End-to-end live smoke: in Telegram, type "goal | grow the org's Twitter | $5 | 2026-07-15" → wait → see decomposition push → see subgoals execute → see cost report → see "goal complete" or "stalled, replanning..." with no terminal interaction.

---

## Final Cleanup (no PR — lands on main after the last PR merges)

- `README.md` — operator-onboarding section: install Ollama, `pip install -e .`, set `AGENTSOS_TELEGRAM_TOKEN`, `agents daemon &`, watch Telegram.
- `ARCHITECTURE.md` — new diagram showing the daemon + reactor + telegram layers above the runtime.
- `AGENTS.md` — done-criteria extended with the daemon smoke test.
- `CHANGELOG.md` — final `[0.3.0]` entry linking the 5 PRs.

---

## Risks & Open Questions

1. **Telegram rate limits** — 30 messages/sec global, 1/sec per chat. The throttler in v0.5.2 should be enough for normal operation. Will need to verify under the end-to-end smoke.
2. **Ollama cold-start latency** — first call after `ollama run` can take 10–30s while the model loads. v0.3's `timeout_s` default is 600, should cover it. May need to bump per-agent timeouts.
3. **SQLite under concurrent writes** — `sqlite3` + WAL mode handles dozens of writers; we'll be at 1–2 (reactor + watchdog). WAL + `PRAGMA busy_timeout=5000` is the standard fix; baked in at store init.
4. **Goal-decomposer hallucinating manifest IDs** — v0.6.1's validation + retry handles this. Worst case: operator sees "decomposition failed, please rephrase" in Telegram.
5. **Cost-guard race condition** — two subgoals spending simultaneously could each check the budget and both proceed. v0.3.3 uses `BEGIN IMMEDIATE` transactions for the cost record; the actual deduction is post-step. Acceptable: small overshoot is fine, the daily ceiling is a tripwire not a hard cap.

## Estimated Time

Roughly 1 PR per 30–45 min of focused work, plus testing and review.
Total: 2.5–4 hours of agent time, but mostly **idle** — 90% of the work
is waiting on pytest + ruff + LLM round-trips. The operator is free.

---

**Status:** Plan saved. Autopilot mode: ACTIVE. Beginning execution with
v0.3 (PR #4) — branch off `feat/checkpoint-resume`, ship the always-on
OS layer.
