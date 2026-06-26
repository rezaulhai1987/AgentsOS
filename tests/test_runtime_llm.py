"""Runtime ↔ LLMClient integration.

Wiring the LLMClient through the Runtime: the provider on the manifest
dispatches to the right adapter, the runtime feeds it messages, and the
resulting `tokens_in` / `tokens_out` are still truthful (computed via the
provider's own usage when reported, else via tokenlab).
"""

from __future__ import annotations

from agentsos.llm.fake import FakeClient
from agentsos.llm_client import Completion, Message
from agentsos.manifest import Manifest
from agentsos.runtime import Runtime


def _stub_manifest(provider: str = "fake") -> Manifest:
    return Manifest(
        name="stub",
        version="0.1.0",
        model={"provider": provider, "id": "fake-model"},
        system_prompt="You are a stub.",
        tools=[],
    )


async def test_runtime_uses_injected_client() -> None:
    """Runtime must accept a client via __init__ so tests can inject fakes."""
    fake = FakeClient()
    fake.record_default(
        Completion(
            message=Message("assistant", "from injected fake"),
            tokens_in=7,
            tokens_out=3,
        )
    )
    rt = Runtime(client=fake)
    r = await rt.run(_stub_manifest(), "hi")
    assert "from injected fake" in r.output
    assert r.tokens_in == 7
    assert r.tokens_out == 3


async def test_get_client_dispatches_fake_for_test_provider() -> None:
    """The registry must hand back a FakeClient when asked for `fake`.
    That's what makes Runtime testable without network calls."""
    from agentsos.llm.fake import FakeClient as _FC
    from agentsos.llm_client import get_client

    client = get_client("fake")
    assert isinstance(client, _FC)

    # And when we put a recorded response on it, Runtime can complete().
    client.record_default(
        Completion(message=Message("assistant", "from dispatch"), tokens_in=1, tokens_out=1)
    )
    rt = Runtime(client=client)
    r = await rt.run(_stub_manifest(provider="fake"), "hello")
    assert r.status == "ok"
    assert "from dispatch" in r.output


async def test_unknown_provider_caught_at_manifest_validation() -> None:
    """The manifest layer is the first line of defence — an unknown
    provider must fail there, before Runtime even gets the manifest."""
    import pydantic

    try:
        Manifest(
            name="stub",
            version="0.1.0",
            model={"provider": "nope-not-real", "id": "x"},
            system_prompt="x",
            tools=[],
        )
    except pydantic.ValidationError as e:
        assert "provider" in str(e)
    else:
        raise AssertionError("expected ValidationError on unknown provider")


async def test_runtime_records_every_llm_call_in_trace(tmp_path) -> None:
    """Observability is part of the contract — every LLM call emits a trace."""
    from agentsos.trace import JsonlTraceSink

    fake = FakeClient()
    fake.record_default(Completion(message=Message("assistant", "ok"), tokens_in=1, tokens_out=1))
    sink = JsonlTraceSink(root=tmp_path)
    rt = Runtime(client=fake, traces=sink)
    await rt.run(_stub_manifest(), "anything")

    # We expect at least: step.started, llm.called, step.completed.
    kinds = []
    for f in tmp_path.glob("*.jsonl"):
        for line in f.read_text().splitlines():
            kinds.append(__import__("json").loads(line)["kind"])
    assert "step.started" in kinds
    assert "llm.called" in kinds
    assert "step.completed" in kinds
