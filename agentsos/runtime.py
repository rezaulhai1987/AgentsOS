"""Runtime: where an agent actually runs.

Owns the Think-Act-Observe loop:
- Spawn: load manifest, get an LLM client for `model.provider`.
- Think: call the LLM with the current transcript + advertised tools.
- Act: if the model returned tool_calls, dispatch each via the tool
  registry, append results as `tool` messages.
- Observe: track per-step tokens / cost / elapsed time.
- Done: when the model returns no tool_calls OR a policy fires.

Policies honoured (all from `manifest.policies`):
- `max_steps` — hard cap on Think-Act-Observe iterations.
- `max_cost_usd` — runtime budget; loop halts once cumulative cost
  exceeds this. v0.2b uses a uniform tokenlab-derived rate ($0.00001 /
  token); per-model pricing lands in v0.3.
- `timeout_s` — wall-clock budget via `asyncio.wait_for`.

Token accounting: provider-reported `tokens_in` / `tokens_out` win when
non-zero; otherwise we fall back to `tokenlab.count` so the cost ceiling
stays truthful even for local adapters that don't report usage.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tokenlab.count import count as count_text
from tokenlab.count import count_messages

from .manifest import Manifest
from .registry import ToolRegistry
from .trace import JsonlTraceSink, TraceEvent

if TYPE_CHECKING:
    from .llm_client import LLMClient, ToolCall, ToolSpec

# Conservative uniform rate used for cost ceiling until v0.3 lands per-model
# pricing. $0.01 per 1k tokens ≈ $0.00001 / token.
TOKEN_USD_RATE = 1e-5  # $0.01 per 1k tokens
# Default cap on a single tool-call's output (chars) to avoid runaway
# context. v0.2c will replace this with tokenlab-based trimming.
MAX_TOOL_OUTPUT_CHARS = 4_000


@dataclass
class RunResult:
    agent: str
    steps: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    status: str
    output: str
    tool_calls: tuple[str, ...] = field(default_factory=tuple)


class Runtime:
    """Owns one agent's lifecycle. Stateless across runs unless a client
    or tool registry is injected; that makes the runtime safe to share
    across agents."""

    def __init__(
        self,
        sandbox: str = "process",
        traces: JsonlTraceSink | None = None,
        client: LLMClient | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        if sandbox != "process":
            raise NotImplementedError(f"sandbox={sandbox!r} arrives in v0.5")
        self.traces = traces or JsonlTraceSink()
        self._client = client
        self._tools = tools or ToolRegistry()

    async def run(self, manifest: Manifest, goal: str) -> RunResult:
        from .llm_client import Message, get_client

        agent_id = f"{manifest.name}#{id(manifest) & 0xFFFF:x}"
        self.traces.emit(
            TraceEvent(agent=agent_id, step=0, kind="step.started", payload={"goal": goal})
        )

        client = self._client or get_client(manifest.model.provider)

        system_msg = Message("system", manifest.system_prompt)
        user_msg = Message("user", goal)
        messages: list[Message] = [system_msg, user_msg]

        tool_specs = self._advertise_tools(manifest.tools)
        total_in = 0
        total_out = 0
        tool_calls_made: list[str] = []
        status = "ok"
        output = ""
        step = 0
        deadline = time.monotonic() + manifest.policies.timeout_s

        for step in range(1, manifest.policies.max_steps + 1):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                completion = await asyncio.wait_for(
                    client.complete(
                        messages,
                        model=manifest.model.id,
                        tools=tool_specs,
                        temperature=manifest.model.temperature,
                        max_tokens=manifest.model.max_tokens,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                status = "timeout_reached"
                break

            tokens_in = completion.tokens_in or sum(
                mc.tokens for mc in count_messages([m.to_dict() for m in messages])
            )
            tokens_out = completion.tokens_out or count_text(completion.message.content)
            total_in += tokens_in
            total_out += tokens_out
            # Cost policy is checked after every step so a runaway loop
            # can't burn through a whole max_steps worth of budget after
            # the ceiling is already crossed.
            if (total_in + total_out) * TOKEN_USD_RATE > manifest.policies.max_cost_usd:
                status = "max_cost_reached"
                break

            self.traces.emit(
                TraceEvent(
                    agent=agent_id,
                    step=step,
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

            if completion.tool_calls:
                # Act + Observe: dispatch every tool call the model asked
                # for, then feed the observations back and loop.
                messages.append(completion.message)
                for tc in completion.tool_calls:
                    observation = self._dispatch_tool(tc, agent_id, step)
                    tool_calls_made.append(tc.name)
                    messages.append(
                        Message(
                            role="tool",
                            content=observation,
                            name=tc.name,
                            tool_call_id=tc.id,
                        )
                    )
                continue

            # No tool calls → final answer.
            output = completion.message.content
            break
        else:
            # Loop exhausted max_steps without breaking.
            status = "max_steps_reached"

        cost_usd = (total_in + total_out) * TOKEN_USD_RATE

        result = RunResult(
            agent=agent_id,
            steps=step,
            tokens_in=total_in,
            tokens_out=total_out,
            cost_usd=round(cost_usd, 6),
            status=status,
            output=output,
            tool_calls=tuple(tool_calls_made),
        )
        self.traces.emit(
            TraceEvent(
                agent=agent_id,
                step=step,
                kind="step.completed",
                payload=json.loads(json.dumps(result.__dict__)),
            )
        )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _advertise_tools(self, names: list[str]) -> list[ToolSpec]:
        from .llm_client import ToolSpec

        specs: list[ToolSpec] = []
        for name in names:
            try:
                tool = self._tools.get(name)
            except KeyError:
                # Unknown tool listed in manifest — skip but don't crash.
                # Strict mode arrives in v0.3.
                continue
            doc_first = (tool.__doc__ or "").strip().splitlines()[0] if tool.__doc__ else ""
            spec = ToolSpec(
                name=name,
                description=doc_first,
                parameters=self._derive_parameters(tool),
            )
            specs.append(spec)
        return specs

    def _dispatch_tool(self, tc: ToolCall, agent_id: str, step: int) -> str:
        try:
            tool = self._tools.get(tc.name)
        except KeyError as e:
            observation = f"[error] {e}"
            self.traces.emit(
                TraceEvent(
                    agent=agent_id,
                    step=step,
                    kind="tool.error",
                    payload={"tool": tc.name, "error": str(e)},
                )
            )
            return observation

        try:
            result = tool(**tc.arguments)
        except Exception as e:  # tool errors must not crash the agent
            observation = f"[error] {tc.name}: {type(e).__name__}: {e}"
            self.traces.emit(
                TraceEvent(
                    agent=agent_id,
                    step=step,
                    kind="tool.error",
                    payload={"tool": tc.name, "error": str(e)},
                )
            )
            return observation

        observation = str(result)
        if len(observation) > MAX_TOOL_OUTPUT_CHARS:
            observation = observation[:MAX_TOOL_OUTPUT_CHARS] + "\n…[truncated]"
        self.traces.emit(
            TraceEvent(
                agent=agent_id,
                step=step,
                kind="tool.called",
                payload={"tool": tc.name, "arguments": tc.arguments, "output": observation},
            )
        )
        return observation

    @staticmethod
    def _derive_parameters(tool) -> dict:
        """Best-effort JSON schema from the tool's signature + annotations.

        Proper extraction arrives in v0.3 via inspect.signature + typing.
        For now we advertise an empty object schema so the model still
        gets the tool's name and description.
        """
        return {"type": "object", "properties": {}, "additionalProperties": True}
