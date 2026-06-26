"""Runtime: where an agent actually runs.

Phase 1: process backend only — spawn a subprocess, send the manifest + goal,
read a JSON line response. The runtime is the I/O boundary; the orchestrator
must not shell out directly.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from tokenlab.count import count as count_text
from tokenlab.count import count_messages

from .manifest import Manifest
from .trace import JsonlTraceSink, TraceEvent


@dataclass
class RunResult:
    agent: str
    steps: int
    tokens_in: int
    tokens_out: int
    status: str
    output: str


class Runtime:
    def __init__(self, sandbox: str = "process", traces: JsonlTraceSink | None = None) -> None:
        if sandbox != "process":
            raise NotImplementedError(f"sandbox={sandbox!r} arrives in v0.5")
        self.traces = traces or JsonlTraceSink()

    async def run(self, manifest: Manifest, goal: str) -> RunResult:
        agent_id = f"{manifest.name}#{id(manifest) & 0xFFFF:x}"
        self.traces.emit(
            TraceEvent(agent=agent_id, step=0, kind="step.started", payload={"goal": goal})
        )
        # Phase 1: stub — produce a deterministic echo so the loop is wired
        # end-to-end. Replaced with a real LLM loop in v0.2.
        await asyncio.sleep(0.01)
        # Real token accounting via tokenlab — the cost ceiling in
        # manifest.policies.max_cost_usd depends on these numbers being truthful.
        system_msg = [{"role": "system", "content": manifest.system_prompt}]
        user_msg = [{"role": "user", "content": goal}]
        tokens_in = count_messages(system_msg)[0].tokens + count_messages(user_msg)[0].tokens
        output_text = f"[stub] agent={manifest.name} model={manifest.model.id} goal={goal!r}"
        tokens_out = count_text(output_text)
        result = RunResult(
            agent=agent_id,
            steps=1,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            status="ok",
            output=output_text,
        )
        self.traces.emit(
            TraceEvent(
                agent=agent_id,
                step=1,
                kind="step.completed",
                payload=json.loads(json.dumps(result.__dict__)),
            )
        )
        return result
