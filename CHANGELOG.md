# Changelog

All notable changes to AgentsOS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-06-26 — Skeleton

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