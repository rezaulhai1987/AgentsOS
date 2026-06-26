# AgentsOS

> An operating system for agent creation, management, and orchestration.

AgentsOS is a framework for building, running, and orchestrating AI agents at
scale. It treats agents as first-class processes — each one has a manifest, a
runtime, a toolbelt, and a place in an orchestration graph — and gives you the
plumbing to spin them up, route work between them, persist their memory, and
observe the whole system in motion.

## Core concepts

| Concept       | What it is                                                              |
|---------------|-------------------------------------------------------------------------|
| **Agent**     | A LLM-backed process with a manifest, toolbelt, and lifecycle hooks.    |
| **Manifest**  | YAML/JSON describing an agent's name, model, tools, prompts, limits.    |
| **Orchestrator** | The kernel — schedules agents, routes events, manages dependencies.  |
| **Runtime**   | Sandbox where an agent executes (Python process, Docker, or remote).   |
| **Tools**     | Discrete capabilities (GitHub, browser, terminal, file, search, etc.).  |
| **Memory**    | Short-term scratch + long-term vector store, shared or per-agent.      |
| **Graph**     | DAG of agents and edges — defines pipelines and reactive flows.         |
| **CLI**       | The shell — `agents` command for the operator.                         |

## Layout

```
AgentsOS/
├── agents/            # manifests + templates
├── orchestrator/      # scheduler, graph, routing
├── runtime/           # sandbox, tool dispatch, LLM client
├── memory/            # store + vector backends
├── tools/             # tool integrations (github, browser, terminal, …)
├── ui/cli/            # `agents` operator CLI
├── tests/             # pytest suite
└── .github/workflows/ # CI
```

## Quickstart

```bash
git clone https://github.com/rezaulhai1987/AgentsOS.git
cd AgentsOS
pip install -e .

# List registered agent templates
agents list-templates

# Spin up an agent from a template
agents run --template research-agent --goal "Summarize today's AI news"

# Run a multi-agent graph
agents run --graph agents/graphs/research-pipeline.yaml
```

## Status

Phase 1 — scaffolding. The skeleton is up; the orchestrator, runtime, and
toolbelt are the next build. See `ARCHITECTURE.md` for the full design and
`ROADMAP.md` for the path from here to a working v0.1.

## License

MIT © rezaulhai1987