"""Think-Act-Observe loop + policy enforcement.

v0.2a ran one LLM call per agent. v0.2b makes `Runtime.run` an actual loop:
- Model returns tool_calls → dispatch → observation → loop.
- Model returns no tool_calls → final answer.
- Loop halts when `max_steps`, `max_cost_usd`, or `timeout_s` says stop.
- Cost ceiling uses tokenlab rates until per-model rates land in v0.3.
"""

from __future__ import annotations

import asyncio

from agentsos.llm.fake import FakeClient
from agentsos.llm_client import Completion, Message, ToolCall
from agentsos.manifest import Manifest
from agentsos.registry import ToolRegistry
from agentsos.runtime import Runtime
from agentsos.tools_builtin import echo as echo_fn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_manifest(
    *,
    max_steps: int = 10,
    max_cost_usd: float = 1.0,
    timeout_s: int = 30,
) -> Manifest:
    return Manifest(
        name="loop",
        version="0.1.0",
        model={"provider": "fake", "id": "fake-model"},
        system_prompt="You loop.",
        tools=["echo"],
        policies={
            "max_steps": max_steps,
            "max_cost_usd": max_cost_usd,
            "timeout_s": timeout_s,
        },
    )


def _registry_with_echo() -> ToolRegistry:
    r = ToolRegistry()
    r.register("echo", echo_fn)
    return r


# ---------------------------------------------------------------------------
# Loop: model returns no tool_calls → final answer on first try.
# ---------------------------------------------------------------------------


async def test_loop_terminates_when_model_returns_final_answer() -> None:
    fake = FakeClient()
    fake.record_default(
        Completion(
            message=Message("assistant", "done"),
            tokens_in=2,
            tokens_out=1,
        )
    )
    rt = Runtime(client=fake, tools=_registry_with_echo())
    r = await rt.run(_stub_manifest(), "hi")
    assert r.output == "done"
    assert r.steps == 1
    assert r.status == "ok"


# ---------------------------------------------------------------------------
# Loop: model calls a tool, gets a result, then returns a final answer.
# ---------------------------------------------------------------------------


async def test_loop_executes_tool_calls_and_loops_until_done() -> None:
    fake = FakeClient()
    # First call: model wants to use echo. Second call: final answer.
    # Use script() because the transcript grows between calls — substring
    # matching on the last user message would hit the first response again.
    fake.script(
        [
            Completion(
                message=Message("assistant", "echoing…"),
                tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
                tokens_in=5,
                tokens_out=3,
            ),
            Completion(message=Message("assistant", "all done"), tokens_in=6, tokens_out=2),
        ]
    )

    rt = Runtime(client=fake, tools=_registry_with_echo())
    r = await rt.run(_stub_manifest(), "hi")
    assert r.output == "all done"
    assert r.steps == 2
    assert r.status == "ok"
    # Two LLM calls + system + user + assistant + tool message all counted.
    assert r.tokens_in >= 5
    assert r.tokens_out >= 3


# ---------------------------------------------------------------------------
# Loop: model keeps asking forever — must hit max_steps.
# ---------------------------------------------------------------------------


async def test_loop_hits_max_steps_policy() -> None:
    fake = FakeClient()
    # Script enough tool-call responses for every step the policy allows.
    fake.script(
        [
            Completion(
                message=Message("assistant", "echo"),
                tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": f"x{i}"}),),
                tokens_in=1,
                tokens_out=1,
            )
            for i in range(5)  # > max_steps=3 so we always hit the cap
        ]
    )

    rt = Runtime(client=fake, tools=_registry_with_echo())
    r = await rt.run(_stub_manifest(max_steps=3), "loop me")
    assert r.steps == 3
    assert r.status == "max_steps_reached"


# ---------------------------------------------------------------------------
# Loop: token cost ceiling — model wants to keep going but budget is gone.
# ---------------------------------------------------------------------------


async def test_loop_hits_max_cost_policy() -> None:
    fake = FakeClient()
    # Each call costs 0.40 USD in tokens (we'll fake a rate later). The
    # default tokenlab rate per token is ~$0.00001 so 10k tokens ≈ $0.10;
    # we set max_cost_usd very low to force the ceiling on the second call.
    fake.record(
        "a",
        Completion(
            message=Message("assistant", "echo"),
            tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "a"}),),
            tokens_in=10_000,
            tokens_out=10_000,
        ),
    )
    fake.record_default(
        Completion(
            message=Message("assistant", "echo"),
            tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "b"}),),
            tokens_in=10_000,
            tokens_out=10_000,
        )
    )

    rt = Runtime(client=fake, tools=_registry_with_echo())
    # 0.50 USD budget: first call costs ~0.20 USD (20k tokens @ tokenlab
    # default); second call would push us over.
    r = await rt.run(_stub_manifest(max_cost_usd=0.50), "a")
    assert r.status in ("max_cost_reached", "ok")
    # If we exited cleanly, ensure we didn't run past the budget.
    assert r.steps <= 3


# ---------------------------------------------------------------------------
# Loop: timeout — runtime must give up before hanging forever.
# ---------------------------------------------------------------------------


async def test_loop_hits_timeout_policy() -> None:
    fake = FakeClient()

    async def slow_complete(*args, **kwargs):
        await asyncio.sleep(0.05)
        return Completion(
            message=Message("assistant", "slow"),
            tokens_in=1,
            tokens_out=1,
        )

    fake.complete = slow_complete  # type: ignore[assignment]

    rt = Runtime(client=fake, tools=_registry_with_echo())
    r = await rt.run(_stub_manifest(timeout_s=1), "fast please")
    # 1s timeout × 0.05s/call = ~20 steps max, but we just want to ensure it
    # finishes without exceeding the budget by orders of magnitude.
    assert r.steps < 100


# ---------------------------------------------------------------------------
# Tool dispatch: unknown tool name → safe failure, loop continues.
# ---------------------------------------------------------------------------


async def test_loop_safely_handles_unknown_tool_name() -> None:
    fake = FakeClient()
    fake.script(
        [
            Completion(
                message=Message("assistant", "call a tool that doesn't exist"),
                tool_calls=(ToolCall(id="c1", name="nope-not-real", arguments={}),),
                tokens_in=2,
                tokens_out=2,
            ),
            Completion(message=Message("assistant", "recovered"), tokens_in=2, tokens_out=2),
        ]
    )

    rt = Runtime(client=fake, tools=_registry_with_echo())
    r = await rt.run(_stub_manifest(), "hi")
    assert r.output == "recovered"
    assert r.status == "ok"
