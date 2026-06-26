"""Runtime: where an agent actually runs.

Owns the agent lifecycle:
- Spawn: load manifest, get an LLM client for `model.provider`.
- Plan: call the LLM with current state.
- Act: dispatch tool calls (v0.2c).
- Observe: append results, check policies (v0.2b).
- Done: emit final answer + structured trace.

Phase 1 (v0.1) shipped a deterministic echo so the loop was wired end-to-end.
Phase 2 (v0.2a) routes real LLM calls through the LLMClient abstraction while
preserving truthful token accounting via the provider's usage report or
tokenlab fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tokenlab.count import count as count_text
from tokenlab.count import count_messages

from .manifest import Manifest
from .trace import JsonlTraceSink, TraceEvent

if TYPE_CHECKING:
    from .llm_client import LLMClient


@dataclass
class RunResult:
    agent: str
    steps: int
    tokens_in: int
    tokens_out: int
    status: str
    output: str


class Runtime:
    """Owns one agent's lifecycle. Stateless across runs unless a client
    is injected; that makes the runtime safe to share across agents."""

    def __init__(
        self,
        sandbox: str = "process",
        traces: JsonlTraceSink | None = None,
        client: LLMClient | None = None,
    ) -> None:
        if sandbox != "process":
            raise NotImplementedError(f"sandbox={sandbox!r} arrives in v0.5")
        self.traces = traces or JsonlTraceSink()
        # `client` is lazy-resolved at run-time so the same Runtime can be
        # used by agents with different providers (test seams).
        self._client = client

    async def run(self, manifest: Manifest, goal: str) -> RunResult:
        agent_id = f"{manifest.name}#{id(manifest) & 0xFFFF:x}"
        self.traces.emit(
            TraceEvent(agent=agent_id, step=0, kind="step.started", payload={"goal": goal})
        )

        from .llm_client import Message, get_client

        client = self._client or get_client(manifest.model.provider)

        system_msg = {"role": "system", "content": manifest.system_prompt}
        user_msg = {"role": "user", "content": goal}
        messages = [system_msg, user_msg]

        # v0.2a: real LLM call through the abstraction. Tool dispatch arrives
        # in v0.2c; for now we just send the conversation.
        completion = await client.complete(
            [Message.from_dict(m) for m in messages], model=manifest.model.id
        )

        # Defensive: providers sometimes report zero usage. Fall back to
        # tokenlab-derived counts so the cost ceiling stays truthful.
        tokens_in = completion.tokens_in
        if tokens_in == 0:
            tokens_in = sum(mc.tokens for mc in count_messages(messages))
        tokens_out = completion.tokens_out
        if tokens_out == 0:
            tokens_out = count_text(completion.message.content)

        self.traces.emit(
            TraceEvent(
                agent=agent_id,
                step=1,
                kind="llm.called",
                payload={
                    "model": manifest.model.id,
                    "provider": manifest.model.provider,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "finish_reason": completion.finish_reason,
                },
            )
        )

        result = RunResult(
            agent=agent_id,
            steps=1,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            status="ok",
            output=completion.message.content,
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
