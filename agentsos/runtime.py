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
- `timeout_s` — wall-clock budget via `asyncio.wait_for`. A fresh
  deadline is granted on `resume()` so a long agent run can be split
  across many invocations without losing the budget.

Token accounting: provider-reported `tokens_in` / `tokens_out` win when
non-zero; otherwise we fall back to `tokenlab.count` so the cost ceiling
stays truthful even for local adapters that don't report usage.

Checkpointing (v0.2d):
- `Runtime.run(..., checkpoint_dir=Path)` writes the scratchpad to
  disk after every step using tmp-file-then-`os.replace` so a SIGKILL
  can't corrupt the checkpoint.
- `Runtime.resume(checkpoint_path)` rebuilds the loop's state from the
  checkpoint JSON and continues. Resume does NOT replay the user
  message — the transcript is restored verbatim from disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tokenlab.count import count as count_text
from tokenlab.count import count_messages

from .manifest import Manifest
from .registry import ToolRegistry
from .trace import JsonlTraceSink, TraceEvent

if TYPE_CHECKING:
    from .llm_client import LLMClient, Message, ToolCall, ToolSpec

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


@dataclass
class _Checkpoint:
    """The on-disk scratchpad. Pure data — no runtime references.

    Kept as a dataclass so tests can construct one in memory if needed
    without round-tripping through JSON.
    """

    agent_name: str
    status: str
    step: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    messages: list[dict[str, Any]]
    tool_calls_made: list[str]
    finished_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "agent_name": self.agent_name,
                "status": self.status,
                "step": self.step,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cost_usd": self.cost_usd,
                "messages": self.messages,
                "tool_calls_made": self.tool_calls_made,
                "finished_at": self.finished_at,
            },
            indent=2,
            sort_keys=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> _Checkpoint:
        d = json.loads(raw)
        return cls(
            agent_name=d["agent_name"],
            status=d["status"],
            step=d["step"],
            tokens_in=d["tokens_in"],
            tokens_out=d["tokens_out"],
            cost_usd=d["cost_usd"],
            messages=list(d["messages"]),
            tool_calls_made=list(d["tool_calls_made"]),
            finished_at=d["finished_at"],
        )


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

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run(
        self,
        manifest: Manifest,
        goal: str,
        *,
        checkpoint_dir: Path | None = None,
    ) -> RunResult:
        """Run the Think-Act-Observe loop from scratch.

        If `checkpoint_dir` is given, a checkpoint JSON file is written
        after every step so a halted run (max_steps / timeout / cost)
        can be resumed via `Runtime.resume(checkpoint_path, manifest)`.
        """
        from .llm_client import Message, get_client

        agent_id = f"{manifest.name}#{id(manifest) & 0xFFFF:x}"
        self.traces.emit(
            TraceEvent(agent=agent_id, step=0, kind="step.started", payload={"goal": goal})
        )

        client = self._client or get_client(manifest.model.provider)

        system_msg = Message("system", manifest.system_prompt)
        user_msg = Message("user", goal)
        messages: list[Message] = [system_msg, user_msg]
        tool_calls_made: list[str] = []
        checkpoint_path = (
            self._new_checkpoint_path(manifest.name, checkpoint_dir)
            if checkpoint_dir is not None
            else None
        )

        result = await self._execute(
            manifest=manifest,
            agent_id=agent_id,
            client=client,
            messages=messages,
            tool_calls_made=tool_calls_made,
            start_step=0,
            checkpoint_path=checkpoint_path,
        )
        # Persist the final checkpoint so the operator can inspect it
        # even on a successful run.
        if checkpoint_path is not None:
            self._write_checkpoint(
                checkpoint_path,
                self._build_checkpoint(manifest.name, result, messages, tool_calls_made),
            )
        return result

    async def resume(self, checkpoint_path: Path, manifest: Manifest) -> RunResult:
        """Resume a halted run from a checkpoint on disk.

        Rebuilds the transcript, token counters, and tool-call log from
        the checkpoint JSON. Does NOT replay the user message — the
        transcript is restored verbatim. A fresh timeout deadline is
        granted so the resumed run gets a full `timeout_s` budget.

        Raises FileNotFoundError if the checkpoint doesn't exist.
        """
        from .llm_client import Message, get_client

        if not await asyncio.to_thread(checkpoint_path.exists):
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_path}")

        raw = await asyncio.to_thread(checkpoint_path.read_text, encoding="utf-8")
        ckpt = _Checkpoint.from_json(raw)
        if ckpt.agent_name != manifest.name:
            raise ValueError(
                f"checkpoint agent_name={ckpt.agent_name!r} does not match "
                f"manifest.name={manifest.name!r}"
            )

        agent_id = f"{manifest.name}#{id(manifest) & 0xFFFF:x}"
        self.traces.emit(
            TraceEvent(
                agent=agent_id,
                step=ckpt.step,
                kind="step.resumed",
                payload={"checkpoint": str(checkpoint_path), "resume_step": ckpt.step + 1},
            )
        )

        client = self._client or get_client(manifest.model.provider)
        messages = [Message.from_dict(m) for m in ckpt.messages]
        tool_calls_made = list(ckpt.tool_calls_made)
        # New checkpoint file for the resumed run so we don't clobber
        # the original on every step.
        resume_path = checkpoint_path.with_name(
            f"{manifest.name}-resume-{self._timestamp()}-{uuid.uuid4().hex[:6]}.json"
        )

        return await self._execute(
            manifest=manifest,
            agent_id=agent_id,
            client=client,
            messages=messages,
            tool_calls_made=tool_calls_made,
            start_step=ckpt.step,
            checkpoint_path=resume_path,
        )

    # ------------------------------------------------------------------
    # Loop core
    # ------------------------------------------------------------------

    async def _execute(
        self,
        *,
        manifest: Manifest,
        agent_id: str,
        client: LLMClient,
        messages: list[Message],
        tool_calls_made: list[str],
        start_step: int,
        checkpoint_path: Path | None,
    ) -> RunResult:
        """The shared Think-Act-Observe loop, used by both run() and resume().

        `start_step` is the count of completed steps BEFORE this call —
        0 for a fresh run, N for a resume. The loop's step counter is
        `(start_step + i)` so the returned `RunResult.steps` reflects
        total work, not work since resume.
        """
        from .llm_client import Message

        tool_specs = self._advertise_tools(manifest.tools)
        total_in = 0
        total_out = 0
        status = "ok"
        output = ""
        step = start_step
        deadline = time.monotonic() + manifest.policies.timeout_s

        # The loop budget is `max_steps` per invocation (run OR resume).
        # The shared `step` counter is global so the returned
        # `RunResult.steps` reflects total work across the run + its
        # resumes; the budget itself is per-call so resume always has
        # room to make progress.
        per_call_budget = manifest.policies.max_steps

        for i in range(1, per_call_budget + 1):
            step = start_step + i
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
                # Persist the mid-loop state so a halt here is resumable.
                self._maybe_checkpoint(
                    checkpoint_path,
                    manifest,
                    _running_checkpoint(
                        manifest.name,
                        step,
                        total_in,
                        total_out,
                        messages,
                        tool_calls_made,
                    ),
                )
                continue

            # finish_reason="length" with empty content means the model
            # was truncated before producing any output — treat it as a
            # keep-going signal (loop counter advances). Without this
            # rule, max_steps_reached would never fire because an empty
            # completion would always break the loop as a final answer.
            if completion.finish_reason == "length" and not completion.message.content:
                messages.append(completion.message)
                self._maybe_checkpoint(
                    checkpoint_path,
                    manifest,
                    _running_checkpoint(
                        manifest.name,
                        step,
                        total_in,
                        total_out,
                        messages,
                        tool_calls_made,
                    ),
                )
                continue

            # No tool calls + non-empty content → final answer.
            # Append the assistant turn to the transcript BEFORE breaking
            # so the checkpoint captures the full conversation history.
            messages.append(completion.message)
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
        # Persist the final state so resume() picks up a consistent view.
        self._maybe_checkpoint(
            checkpoint_path,
            manifest,
            self._build_checkpoint(manifest.name, result, messages, tool_calls_made),
        )
        return result

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _timestamp() -> str:
        """Filename-safe ISO-ish timestamp. Colons stripped for Windows
        and POSIX filesystems that don't allow them."""
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    def _new_checkpoint_path(self, agent_name: str, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{agent_name}-{self._timestamp()}-{uuid.uuid4().hex[:6]}.json"

    @staticmethod
    def _build_checkpoint(
        agent_name: str,
        result: RunResult,
        messages: list[Message],
        tool_calls_made: list[str],
    ) -> _Checkpoint:
        return _Checkpoint(
            agent_name=agent_name,
            status=result.status,
            step=result.steps,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            messages=[m.to_dict() for m in messages],
            tool_calls_made=list(tool_calls_made),
            finished_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _write_checkpoint(path: Path, ckpt: _Checkpoint) -> None:
        """Atomic write: tmp file + os.replace. Survives SIGKILL because
        `os.replace` is atomic on POSIX and Windows — readers always see
        either the old checkpoint or the new one, never a half-written
        file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(ckpt.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def _maybe_checkpoint(
        cls,
        path: Path | None,
        manifest: Manifest,
        ckpt: _Checkpoint,
    ) -> None:
        if path is None:
            return
        cls._write_checkpoint(path, ckpt)

    # ------------------------------------------------------------------
    # Tool dispatch (unchanged from v0.2b)
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


def _running_checkpoint(
    agent_name: str,
    step: int,
    total_in: int,
    total_out: int,
    messages: list[Message],
    tool_calls_made: list[str],
) -> _Checkpoint:
    """Build a checkpoint snapshot for an in-progress loop iteration.

    `status` is left as the empty string here — the run is still in
    flight. The final checkpoint (built by `_build_checkpoint` after
    the loop exits) carries the real status.
    """
    return _Checkpoint(
        agent_name=agent_name,
        status="",  # still running; final status written at exit
        step=step,
        tokens_in=total_in,
        tokens_out=total_out,
        cost_usd=round((total_in + total_out) * TOKEN_USD_RATE, 6),
        messages=[m.to_dict() for m in messages],
        tool_calls_made=list(tool_calls_made),
        finished_at=datetime.now(UTC).isoformat(),
    )
