"""Tests for the runtime stub."""

from __future__ import annotations

from agentsos.manifest import Manifest
from agentsos.runtime import Runtime
from tokenlab.count import count as tl_count


def _stub_manifest() -> Manifest:
    return Manifest(
        name="stub",
        version="0.1.0",
        model={"provider": "openai", "id": "gpt-4o-mini"},
        system_prompt="You are a stub.",
        tools=[],
    )


async def test_runtime_stub_returns_ok() -> None:
    rt = Runtime()
    r = await rt.run(_stub_manifest(), "hello world")
    assert r.status == "ok"
    assert r.steps == 1
    assert "stub" in r.output


async def test_runtime_emits_traces(tmp_path) -> None:
    from agentsos.trace import JsonlTraceSink

    sink = JsonlTraceSink(root=tmp_path)
    rt = Runtime(traces=sink)
    await rt.run(_stub_manifest(), "trace me")
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


async def test_runtime_accounts_real_tokens_via_tokenlab() -> None:
    """Regression: the stub used to use len(goal.split()) which is word count.

    For unattended operation the cost ceiling depends on truthful token
    accounting, so the runtime must call tokenlab.count and the counts must
    agree with tokenlab's own output.
    """
    rt = Runtime()
    manifest = _stub_manifest()
    goal = "Please summarise this long document carefully and report back."
    r = await rt.run(manifest, goal)

    expected_in = tl_count(manifest.system_prompt) + 4 + tl_count(goal) + 4
    expected_out = tl_count(r.output)
    assert r.tokens_in == expected_in
    assert r.tokens_out == expected_out
    # Sanity: stub output is short, so tokens_out should be small (< 50).
    assert r.tokens_out < 50
