# AgentsOS Architecture

## 1. Goals

- **Create** agents from declarative manifests — no boilerplate code.
- **Manage** their full lifecycle: spawn → run → suspend → resume → terminate.
- **Orchestrate** them as nodes in a graph with deterministic scheduling.
- **Observe** everything: traces, logs, costs, tool calls, memory hits.

## 2. Layered model

```
                ┌──────────────────────────────────────────────┐
                │                  UI / CLI                    │   ← operator surface
                ├──────────────────────────────────────────────┤
                │              Orchestrator Kernel             │
                │  scheduler · graph · event bus · router     │
                ├──────────────────────────────────────────────┤
                │                  Runtime                     │
                │   sandbox · tool dispatch · LLM client      │
                ├──────────────────────────────────────────────┤
                │   Memory      │        Tools                 │
                │  scratchpad   │   github · browser · shell    │
                │  vector store │   file · http · code-exec    │
                └──────────────────────────────────────────────┘
```

## 3. Agent lifecycle

```
  manifest  ──►  spawn  ──►  plan  ──►  act  ──►  observe  ──►  done
                     │          │         │          │
                     ▼          ▼         ▼          ▼
                 registry    memory    tool call   trace log
                 health     scratch   result     memory write
```

Each agent instance carries:
- `manifest` — immutable identity (model, tools, system prompt, limits)
- `state`   — mutable (messages, scratchpad, current step, tokens used)
- `policies` — retry budgets, max steps, cost cap, timeout

## 4. Orchestrator

The orchestrator owns the **event bus** and the **graph**. It is event-sourced:
agents emit events (`step.started`, `tool.called`, `step.completed`, …),
the router decides what to do next, and the scheduler ticks work onto
runtimes.

### 4.1 Scheduling

- **Priority queue** keyed by `graph.priority`.
- **Concurrency cap** per agent class (prevents accidental fork-bomb).
- **Backpressure**: queue full → caller gets `BUSY` event.

### 4.2 Routing

Edges in a graph carry:
- `when` — predicate over the event payload
- `target` — node id, agent template, or `end`
- `merge` — how to combine multiple branches (all / any / first)

### 4.3 Reactive flows

Beyond imperative graphs, an agent can publish to named topics; any agent
with a matching subscription fires on the next tick. Topics are first-class —
they're how the system composes at runtime.

## 5. Runtime

The runtime is what actually executes an agent's plan.

- **Sandbox backends**:
  - `process` — same-host subprocess (cheap, no isolation)
  - `docker`  — containerized (default for untrusted code)
  - `remote`  — POSTs to a remote runtime endpoint
- **Tool dispatch**: the runtime exposes a tool registry. Each tool is a
  callable with a JSON schema and an allowlist of agents that may invoke it.
- **LLM client**: a thin abstraction over OpenAI-compatible APIs, Anthropic,
  llama.cpp, and local HF models. Tokens are accounted per agent.

## 6. Memory

Two tiers:

1. **Scratchpad** — per-agent, in-process, ephemeral. Holds the current
   plan, intermediate observations, and the next-step hypothesis.
2. **Long-term store** — keyed by `agent_id` + `namespace`. Pluggable
   backend:
   - `sqlite`  — embedded, zero-config
   - `pgvector` — Postgres with vector column
   - `chroma` / `qdrant` — purpose-built vector DBs

Retrieval is automatic: before each LLM call, the runtime pulls the top-k
relevant memories and prepends them to the prompt context window.

## 7. Tools

Each tool lives in `tools/<name>.py` and exposes:

```python
class Tool:
    name: str
    description: str
    schema: dict        # JSON schema for arguments
    allowlist: list[str] | None   # agent templates that may call this

    async def run(self, **kwargs) -> ToolResult: ...
```

Built-in starter set:
- `github`  — issues, PRs, file ops
- `browser` — headless Chrome via playwright
- `terminal` — shell command exec with allowlist
- `file`     — local FS read/write
- `http`     — fetch with timeout + size cap
- `code-exec` — sandboxed Python

## 8. Observability

Every step produces a structured trace:

```json
{
  "agent": "research-agent#7",
  "step": 3,
  "started": "2026-06-26T08:31:00Z",
  "duration_ms": 1240,
  "model": "claude-sonnet-4",
  "tokens_in": 1820, "tokens_out": 340,
  "tool_calls": [{"name": "http", "args": {...}, "result_size": 4096}],
  "memory_hits": 2,
  "status": "ok"
}
```

Traces are written to a local JSONL sink by default; a future Prometheus
exporter ships with v0.2.

## 9. Extension points

- New tool → drop a file in `tools/`, register in `tools/__init__.py`.
- New agent template → YAML in `agents/templates/`.
- New runtime backend → subclass `runtime.sandbox.Sandbox`.
- New memory backend → subclass `memory.store.Store`.
- New graph type → subclass `orchestrator.graph.GraphRunner`.

## 10. Non-goals (for v0.x)

- Multi-tenant auth — single operator assumed.
- GPU scheduling — runs on whatever machine you point it at.
- A polished web UI — the CLI is the surface; web UI is later.
- Reinforcement learning or fine-tuning — AgentsOS is a runtime, not a lab.