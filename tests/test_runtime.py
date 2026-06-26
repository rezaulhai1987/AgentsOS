"""Tests for the runtime stub.

The stub was retired in v0.2a — Runtime now routes every call through
LLMClient. These tests inject a FakeClient so we don't need a network.
"""

from __future__ import annotations

from agentsos.llm.fake import FakeClient
from agentsos.llm_client import Completion, Message
from agentsos.manifest import Manifest
from agentsos.runtime import Runtime
from tokenlab.count import count as tl_count


def _stub_manifest() -> Manifest:
    return Manifest(
        name="stub",
        version="0.1.0",
        model={"provider": "fake", "id": "fake-model"},
        system_prompt="You are a stub.",
        tools=[],
    )


async def test_runtime_runs_through_client() -> None:
    fake = FakeClient()
    fake.record_default(
        Completion(
            message=Message("assistant", "[stub] agent=stub model=fake-model goal='hello world'"),
            tokens_in=0,
            tokens_out=0,
        )
    )
    rt = Runtime(client=fake)
    r = await rt.run(_stub_manifest(), "hello world")
    assert r.status == "ok"
    assert r.steps == 1
    assert "stub" in r.output


async def test_runtime_emits_traces(tmp_path) -> None:
    from agentsos.trace import JsonlTraceSink

    fake = FakeClient()
    fake.record_default(Completion(message=Message("assistant", "ok"), tokens_in=1, tokens_out=1))
    sink = JsonlTraceSink(root=tmp_path)
    rt = Runtime(client=fake, traces=sink)
    await rt.run(_stub_manifest(), "trace me")
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


async def test_runtime_accounts_real_tokens_via_tokenlab() -> None:
    """Regression: the stub used to use len(goal.split()) which is word count.

    For unattended operation the cost ceiling depends on truthful token
    accounting. FakeClient falls back to tokenlab when tokens_in/tokens_out
    are 0, so the runtime's counts must match tokenlab's own output.
    """
    fake = FakeClient()
    fake.record_default(
        Completion(message=Message("assistant", "from injected fake"), tokens_in=0, tokens_out=0)
    )
    rt = Runtime(client=fake)
    manifest = _stub_manifest()
    goal = "Please summarise this long document carefully and report back."
    r = await rt.run(manifest, goal)

    # tokens_in = system + user (overhead is 4 tokens per message for cl100k).
    expected_in = tl_count(manifest.system_prompt) + 4 + tl_count(goal) + 4
    expected_out = tl_count(r.output)
    assert r.tokens_in == expected_in
    assert r.tokens_out == expected_out
    # Sanity: output is short.
    assert r.tokens_out < 50
