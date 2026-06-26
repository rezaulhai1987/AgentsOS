# Roadmap

## v0.1 — Skeleton (this commit)
- [x] Repo, docs, layout, CI scaffold
- [x] Agent manifest schema
- [x] Empty orchestrator / runtime / memory / tools packages
- [ ] `agents` CLI with `list-templates` and `run --template`

## v0.2 — Single-agent runtime
- [ ] Runtime sandbox (`process` backend)
- [ ] LLM client (OpenAI-compat + Anthropic + llama.cpp)
- [ ] Tool dispatch loop (think → act → observe)
- [ ] Scratchpad memory
- [ ] Trace sink (JSONL)

## v0.3 — Orchestration
- [ ] Event bus
- [ ] Scheduler + priority queue
- [ ] Graph runner (DAG execution)
- [ ] Reactive topics

## v0.4 — Memory
- [ ] SQLite store
- [ ] Vector retrieval (sqlite-vec or chroma)
- [ ] Auto-retrieve before LLM call

## v0.5 — Tooling
- [ ] github, browser, terminal, file, http, code-exec
- [ ] Per-tool allowlists
- [ ] Tool sandboxing (Docker backend)

## v0.6 — Observability + UX
- [ ] Trace explorer (`agents traces`)
- [ ] `agents graph` visualizer
- [ ] Cost dashboard

## v1.0 — Stable
- [ ] Backwards-compatible manifest schema v1
- [ ] Plugin system for third-party tools/backends
- [ ] Multi-runtime deployment (workers + central bus)
- [ ] Security audit pass