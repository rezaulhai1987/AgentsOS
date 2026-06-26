# Roadmap

> End-state vision: an **unattended, autonomous agent operating system**. Every
> layer below is in service of that — policies are enforced, traces are
> structured, state is checkpointed, and the cost ceiling is real so an agent
> can run for hours without burning the wallet or the operator's trust.

## v0.1 — Skeleton (shipped 2026-06-26)
- [x] Repo, docs, layout, CI scaffold
- [x] Agent manifest schema (Pydantic + YAML)
- [x] Orchestrator / runtime / memory / tools packages with primitives
- [x] `agents` CLI: `list-templates`, `validate`, `run`, `tools`
- [x] Process-sandboxed runtime with JSONL trace sink
- [x] Built-in tools: `echo`, `read_file`, `write_file`, `list_dir`
- [x] **tokenlab v0.1.0** — token counting, budgeting, compression, caching,
  router, schema minimisation, trim (98% reduction on the demo transcript)
- [x] `tools/audit_session.py` — Hermes session auditor (standalone)

## v0.2 — Single-agent runtime (next)
The runtime needs to do real work. Stub returns are gone after this milestone.
- [ ] Provider-agnostic LLM client (openai / anthropic / llama.cpp)
- [ ] Think → Act → Observe loop with policy enforcement
  - `max_steps`, `max_cost_usd`, `timeout_s` — abort cleanly on ceiling
- [ ] Token accounting via tokenlab on every step (not stub word-count)
- [ ] Tool dispatch with JSON-schema tool calls, traced per invocation
- [ ] Checkpointing + resume (scratchpad persisted, rehydratable after crash)

## v0.3 — Orchestration
- [ ] Wire event bus, scheduler, graph runner to real runtime
- [ ] Multi-agent DAGs actually execute unattended
- [ ] Reactive topics (`on`, `when`, `after`)
- [ ] Dead-letter queue for failed branches

## v0.4 — Memory
- [ ] SQLite store (long-term)
- [ ] Vector retrieval (sqlite-vec or chroma)
- [ ] Auto-retrieve before each LLM call
- [ ] Namespaced scratchpad (per spec in manifest)

## v0.5 — Tooling
- [ ] github, browser, terminal, http, code-exec
- [ ] Per-agent tool allowlists (from manifest)
- [ ] Docker sandbox backend (replace `process`)

## v0.6 — Observability + UX
- [ ] Trace explorer (`agents traces`)
- [ ] `agents graph` visualizer
- [ ] Cost dashboard (powered by tokenlab)
- [ ] Watchdog for unattended runs (alert on dead agent, cost overrun)

## v1.0 — Stable
- [ ] Backwards-compatible manifest schema v1
- [ ] Plugin system for third-party tools/backends
- [ ] Multi-runtime deployment (workers + central bus)
- [ ] Security audit pass