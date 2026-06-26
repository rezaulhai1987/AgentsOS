"""Tests for the runtime stub."""

from __future__ import annotations

from agentsos.manifest import Manifest
from agentsos.runtime import Runtime


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
