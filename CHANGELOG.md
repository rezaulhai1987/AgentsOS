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

### Changed
- Runtime now accounts tokens via `tokenlab.count` instead of word-splitting.
  `tokens_in` / `tokens_out` on `RunResult` are now truthful and suitable for
  cost-ceiling enforcement in v0.2.