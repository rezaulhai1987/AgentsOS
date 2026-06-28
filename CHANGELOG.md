# Changelog

All notable changes to AgentsOS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.3.7] — 2026-06-28 — Desktop surface + TAO rename

### Added
- **`agentsos/telegram/desktop.py`** — Windows-OS-style surface for
  the operator's phone. 310 lines of pure renderers, no network:
  - `render_desktop(snapshot)` — single big card laid out as a
    window: TOP BAR (clock/uptime/cost/queue/agents), LEFT PANE
    (task tree with progress bars), RIGHT PANE (journal tail),
    BOTTOM (next-action hint).
  - `render_tree(rows)` — full goal tree (parent → children) with
    status, depth indent, and progress bars.
  - `render_files(path, base)` — directory listing with emoji
    prefixes (📁 dirs / 📄 files + size).
  - `render_plan(plan)` — manifest + DAG view (steps with
    `agent=` and `deps=` columns).
  - `render_log(lines, n)` — daemon JSONL tail.
  - `render_help_extended()` — operator reference card covering
    every command incl. /pause /resume /cancel /stop.
  - `build_keyboard()` — 4-row inline Telegram keyboard (12
    buttons + help). Designed to fit any phone screen without
    scrolling. Button taps emit `cmd:<name>` callback data that
    the bridge will dispatch to the same renderers.
- **Brand rename JARVIS → TAO** across the whole Telegram stack
  (bot.py docstring, hud.py docstring + headers + help text,
  tests, smoke script). No "JARVIS" string remains in the source
  tree — verified with `grep -r "JARVIS" agentsos/`.

### Verified
- `pytest -q` — 138/138 passed in 31.7s.
- `desktop.py` imports cleanly under the package init.
- `grep -r "JARVIS" agentsos/` returns zero matches.

## [0.3.6] — 2026-06-28 — Crash-resilient work journal + Telegram bridge

### Added
- **`agentsos/work_registry.py`** — crash-resilient state spine (v0.3.5+).
  - `Journal` writes every daemon event as a JSONL line with atomic
    `os.replace` + `fsync`. A SIGKILL mid-step leaves the previous
    entry intact; a corrupted trailing line is dropped on read.
  - `Registry` is the "where are we right now" snapshot — current
    task, next task, open PRs, head commit. Atomic flush + `.bak`
    recovery so a partial write never poisons the registry file.
  - 26/26 tests in `test_work_registry.py` + `test_telegram_hud.py`
    cover enum-value normalization, empty-list guards, and initial
    snapshot flush.
- **Daemon ↔ work_registry wiring** (v0.3.6). `Daemon.__init__` now
  creates a `Journal` at `<state_dir>/journal.jsonl` and a `Registry`
  at `<state_dir>/registry.json`. Every event written via
  `_log_event` is mirrored into the journal so a crash-resume sees
  the full timeline. `Daemon.snapshot()` now exposes the registry
  head (branch, current_task_id, next_task_id, tasks count, open
  PRs) plus a journal entry count.
- **`agentsos/telegram/bridge.py`** — the single glue point between
  the daemon and the TAO Telegram bot. `attach_bridge(token,
  chat_id)` returns an `extra_task` factory that subscribes the
  notifier to watchdog + cost-guard and starts the long-poll
  `TelegramBot`. Reads `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
  from env. Gracefully no-ops when the optional dep is missing or
  the env vars are unset so the daemon still boots in air-gapped
  environments.
- **10 new tests**: 6 in `test_daemon_journal.py` (init files exist,
  journal entries mirrored from watchdog, crash-resume preserves
  journal, registry survives restart), 4 in `test_telegram_bridge.py`
  (no-op without env, graceful degradation, empty-token handling).
- **Test count: 134 passing** (up from 67 in v0.2d).

### Notes
- The journal is the spine for `tail -f`/Telegram `/log` and for
  `Registry.compute_next_actions()` after a crash. The registry is
  the spine for `agents status` and the TAO `/live` card. Both
  live under `<state_dir>/` so `restart` resumes from disk with no
  operator intervention.
- The Telegram bridge does NOT swallow exceptions silently — any
  uncaught error during `run_forever()` is logged at WARNING so an
  operator scanning the daemon JSONL notices the bridge is offline.
- The Telegram bot token was stored in `~/.hermes/.env` (chmod 600)
  rather than in the repo or the chat transcript; the CLI reads it
  via `os.environ` at boot. Future: move to a vault like
  `keyring`/`pass`.

## [0.2.3] — 2026-06-28 — Checkpoint + resume (v0.2d)

### Added
- **`Runtime.run(checkpoint_dir=Path)`** writes the full scratchpad to
  disk after every step. The scratchpad captures the transcript
  (`messages`), token counters (`tokens_in` / `tokens_out`), tool-call
  log (`tool_calls_made`), running step counter, and status. Filenames
  follow `<agent_name>-<UTC timestamp>-<short uuid>.json` so a directory
  of checkpoints is naturally ordered and never collides on parallel
  runs of the same agent.
- **`Runtime.resume(checkpoint_path, manifest)`** rebuilds the
  transcript and counters from the checkpoint JSON and continues the
  loop. Resume does NOT replay the user message — the conversation is
  restored verbatim from disk. A fresh `timeout_s` deadline is granted
  on each resume so a long agent can be split across many invocations
  without losing the budget. Resume writes its own checkpoint file
  (`<agent_name>-resume-<UTC timestamp>-<short uuid>.json`) so the
  original halt state is preserved for forensics.
- **Atomic write**: `tmp` + `os.replace()`. Readers always see either
  the old checkpoint or the new one — never a half-written file. A
  SIGKILL mid-step leaves the previous checkpoint intact.
- **`finish_reason="length"` with empty content** is no longer treated
  as a final answer. The loop now appends the empty assistant turn to
  the transcript and continues. This is required for `max_steps_reached`
  to ever fire: without the rule, an empty truncated completion would
  break the loop as a final answer with `status="ok"` and `output=""`.
- **Per-call step budget**: each invocation (run OR resume) gets its own
  `max_steps` budget, while the returned `RunResult.steps` is the
  *total* steps across the run + any resumes. A `max_steps=1` agent
  that halts at step 1 and is resumed with `max_steps=1` makes 1 more
  step and reports `steps=2`.
- **6 new tests** (`tests/test_checkpoint.py`): file exists after run,
  payload is valid JSON with the expected shape, resume picks up at
  the last step without replaying the user message, resume uses the
  next scripted completion, resume of a non-existent checkpoint
  raises `FileNotFoundError`, checkpoint writes are atomic (no `.tmp`
  sibling left behind).
- **Test count: 67 passing** (up from 61 in v0.2c).

### Notes
- The checkpoint JSON schema is intentionally lossy-free: every field
  required to reconstruct a run is present, and nothing in the
  runtime's internal state is required to resume. v0.2e will add a
  `Runtime.checkpoint_info(path)` helper for inspecting a checkpoint
  without resuming it.
- `datetime.UTC` (PEP 615) replaces `datetime.timezone.utc` for the
  python-3.11 floor in `pyproject.toml`.
- `pathlib` I/O inside the async `resume()` is wrapped in
  `asyncio.to_thread` to satisfy `ASYNC240`; the runtime remains a
  thin orchestrator and does not block the event loop on disk reads.

## [0.2.2] — 2026-06-26 — Free / local LLM support (Ollama)

### Added
- **Ollama adapter** (`agentsos.llm.ollama`). `provider: ollama` in the
  manifest now resolves to `http://localhost:11434/v1` with no API key
  required. Backed by the existing OpenAI-compat wire format — no
  duplication of chat-completions logic.
- **`ModelSpec.base_url` and `ModelSpec.api_key`** — per-agent overrides
  for any provider. Useful when Ollama runs on a remote host
  (`base_url: http://gpu-box.lan:11434/v1`) or when a hosted endpoint
  needs its own key distinct from the global env var.
- **`AGENTSOS_OLLAMA_BASE_URL`** env var — runtime override for users
  who can't edit YAML (CI runners, sidecar containers).
- **`agents/models/REGISTRY.md`** — ranked top-10 free/open-weight LLMs
  for agent workloads (Llama 3.3 70B, DeepSeek-R1/V3, Qwen 2.5 72B,
  Mistral Large 2, Llama 3.1 8B, Phi-4, Gemma 3 27B, Command-R,
  Yi-1.5 34B, gpt-oss-20b/120b) with license, hardware, and
  fit-for-agents reasoning per entry.
- **`agents/templates/local-llama-agent.yaml`** — drop-in starter
  manifest using `provider: ollama`.

### Changed
- `ModelSpec.provider` regex now accepts `ollama` alongside the existing
  `openai | anthropic | llama.cpp | hf | fake`. `fake` remains the test
  escape hatch and is never valid in real agent YAML.

### Notes
- **All models in `REGISTRY.md` are free at the point of use** when run
  via local Ollama. The `max_cost_usd` policy remains truthful
  (local inference is $0; the uniform token-rate placeholder still
  fires as a safety ceiling). For hosted free tiers (OpenRouter,
  Groq) the ceiling becomes meaningful once per-model pricing ships
  in v0.3.

## [0.2.1] — 2026-06-26 — Think-Act-Observe loop

### Added
- Repository layout: `agents/`, `orchestrator/`, `runtime/`, `memory/`,
  `tools/`, `ui/cli/`, `tests/`.
- Agent manifest schema (YAML + JSON schema).
- Three starter templates: `research-agent`, `code-agent`, `orchestrator`.
- `agents` CLI: `list-templates`, `validate`, `run`, `tools`.
- Pydantic-validated manifest loader with `@path` prompt resolution.
- Priority scheduler, in-process event bus, and graph runner
  (orchestrator primitives).
- Process-sandboxed runtime with structured trace events.
- Built-in tools: `echo`, `read_file`, `write_file`, `list_dir`.
- **tokenlab** — token counting, budgeting, compression, response caching,
  schema minimisation, router, and tool-message trim. 98% reduction on the
  bundled long-running-transcript demo.
- `tools/audit_session.py` — standalone Hermes session auditor.
- Pytest suite for manifest, orchestrator, runtime, tools, and tokenlab.
- GitHub Actions CI (lint + test on Python 3.11/3.12).

## [0.2.1] — 2026-06-26 — Think-Act-Observe loop

### Added
- Runtime now owns a real loop: Think (LLM call) → Act (dispatch every
  `tool_call`) → Observe (append `tool` message) → repeat until the
  model stops calling tools or a policy fires.
- `Runtime.run` honours every policy in `manifest.policies`:
  - `max_steps` — hard cap on loop iterations.
  - `max_cost_usd` — checked **inline after each step**, not at exit, so
    a runaway loop can't burn through a full `max_steps` after the
    ceiling is already crossed.
  - `timeout_s` — wall-clock budget via `asyncio.wait_for`.
- `FakeClient.script([Completion, ...])` — ordered-queue scripting for
  deterministic multi-turn loop tests (the older `record()` /
  `record_default()` substring-match API still works for single-turn
  tests, but breaks once the transcript grows).
- `Completion.tool_calls` / `Completion.finish_reason` — promoted to
  first-class fields on the `LLMClient` contract so adapters can
  report structured function calls without overloading `content`.
- `RunResult` — structured return from `Runtime.run` carrying the full
  accounting picture (steps, tokens in/out, cost_usd, status, output,
  tool_calls).
- New `tools_builtin.py` docstrings are read at runtime to build
  `ToolSpec.description` (first non-empty line of the docstring).
- Trace events: `step.started`, `llm.called`, `tool.called`,
  `tool.error`, `step.completed` — every transition a real agent run
  produces is on the JSONL trace sink.

### Fixed
- Runtime previously called the LLM once and exited; it now loops and
  dispatches tool calls, making agents actually executable end-to-end.
- `ToolRegistry` no longer crashes when a manifest references an
  unknown tool — the spec is just skipped (strict mode lands in v0.3).

## [0.2.0] — 2026-06-26 — LLM client abstraction

### Added
- `agentsos.llm_client` — provider-agnostic `LLMClient` ABC, `Completion`,
  `Message`, `ToolCall`, `ToolSpec` dataclasses, plus `register_client` /
  `get_client` registry.
- `agentsos.llm.fake.FakeClient` — in-memory client for tests; supports
  `record(key, completion)` for per-prompt responses and `record_default`
  as a catch-all fallback.
- `agentsos.llm.openai_compat.OpenAICompatClient` — HTTP adapter covering
  OpenAI, OpenRouter, vLLM, llama.cpp server, Groq, Together, and any
  other `/v1/chat/completions` endpoint. Provider-reported usage is
  honoured; missing usage falls back to `tokenlab.count`.
- `Manifest.model.provider` now accepts `fake` for tests; real YAMLs
  remain restricted to `openai|anthropic|llama.cpp|hf`.
- `httpx` re-added to dependencies (was previously transitively pulled;
  v0.2 makes it a first-class dep).
- Tests: 49 passing (10 new for the LLMClient, 4 for OpenAI-compat, 4 for
  Runtime ↔ LLMClient integration, 1 expanded manifest validation).

### Changed
- `Runtime` now routes every agent run through an `LLMClient`. The
  Phase-1 deterministic echo is gone; agents without a client are looked
  up via `get_client(manifest.model.provider)`. The stub-style
  `test_runtime.py` tests were updated to inject a `FakeClient`.
- `Runtime.run` emits an `llm.called` trace event with model, provider,
  tokens_in/out, and finish_reason alongside `step.started` and
  `step.completed`.

### Changed
- Runtime now accounts tokens via `tokenlab.count` instead of word-splitting.
  `tokens_in` / `tokens_out` on `RunResult` are now truthful and suitable for
  cost-ceiling enforcement in v0.2.