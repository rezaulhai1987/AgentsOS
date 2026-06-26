# AGENTS.md — how to work in this repo

Any agent (human or AI) dropped into AgentsOS is expected to follow these
conventions. They make the codebase consistent across many hands.

## 1. Read first
- `README.md` — what this is.
- `ARCHITECTURE.md` — how it's put together.
- `ROADMAP.md` — what's next.

## 2. Directory rules
- **Adding a tool?** → `tools/<name>.py` with the `Tool` interface.
- **Adding an agent template?** → `agents/templates/<name>.yaml`.
- **Adding to the orchestrator?** → `orchestrator/<module>.py`. Pure logic;
  no I/O outside of the event bus.
- **Adding to the runtime?** → `runtime/<module>.py`. Anything that touches
  the network, shell, or filesystem lives here.
- **Adding tests?** → `tests/test_<module>.py`, mirror the source path.

## 3. Code style
- Python 3.11+, type hints everywhere.
- `ruff` for lint + format (`ruff check . && ruff format .`).
- `pytest` for tests; one assertion theme per test.
- Async by default for I/O (`async def`, `await`).

## 4. Commit messages
Conventional Commits, scoped:
- `feat(orchestrator): add priority queue`
- `fix(runtime): cap tool output to 64KB`
- `docs(architecture): clarify routing rules`

## 5. Branches & PRs
- `main` is always green. No direct pushes.
- One feature per branch: `feat/<short-name>`.
- PR title = commit title. Body explains *why*, not *what*.

## 6. Agent templates
YAML, not code. Schema lives in `agents/schema.json`. A template must declare:
- `name`, `version`, `description`
- `model` (provider + model id)
- `system_prompt` (string or path to `.md`)
- `tools` (list of tool names from the registry)
- `policies` (max_steps, max_cost_usd, timeout_s)

## 7. Definition of done
For any feature:
- [ ] Code in the right layer (see §2).
- [ ] Tests covering the happy path + at least one edge case.
- [ ] `ruff` clean.
- [ ] `pytest` green.
- [ ] `ROADMAP.md` updated if a phase closes.
- [ ] PR open with conventional commit title.