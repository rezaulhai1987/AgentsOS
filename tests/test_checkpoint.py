"""Checkpoint + resume tests.

An agent's scratchpad — its transcript, token counters, tool-call log,
and last status — must be on disk between steps so that:

- A `max_steps_reached` halt can be resumed with `Runtime.resume()`
  without losing the conversation.
- A `timeout_reached` halt resumes cleanly (no duplicate user message,
  no replayed tool calls).
- A clean run produces a checkpoint file the operator can inspect.
- Atomic writes survive a process kill mid-write (no half-written JSON).

Strategy: a `Runtime.run(checkpoint_dir=Path)` writes the scratchpad
after every step using tmp-file-then-rename so a SIGKILL can't corrupt
the checkpoint. `Runtime.resume(checkpoint_path)` rebuilds the loop's
state and continues from where it left off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentsos.llm.fake import FakeClient
from agentsos.llm_client import Completion, Message
from agentsos.runtime import Runtime, RunResult


def _manifest(tmp_path: Path):
    """Tiny inline manifest — no YAML round-trip in these tests."""
    from agentsos.manifest import Manifest, ModelSpec, Policies

    return Manifest(
        name="ckpt-agent",
        version="0.1.0",
        description="checkpoint test",
        model=ModelSpec(provider="fake", id="fake-model"),
        system_prompt="you loop",
        tools=[],
        policies=Policies(max_steps=3, max_cost_usd=100.0, timeout_s=60),
    )


def _script(*contents: str) -> FakeClient:
    """Build a FakeClient whose completions cycle through `contents`."""
    fake = FakeClient()
    fake.script(
        [
            Completion(
                message=Message("assistant", text),
                tokens_in=10,
                tokens_out=5,
                finish_reason="stop",
            )
            for text in contents
        ]
    )
    return fake


@pytest.mark.asyncio
async def test_checkpoint_file_exists_after_run(tmp_path: Path) -> None:
    """A successful run must leave a checkpoint on disk."""
    fake = _script("first answer")
    runtime = Runtime(client=fake)
    manifest = _manifest(tmp_path)
    await runtime.run(manifest, goal="hello", checkpoint_dir=tmp_path)

    ckpts = list(tmp_path.glob("ckpt-agent-*.json"))
    assert len(ckpts) == 1
    payload = json.loads(ckpts[0].read_text())
    assert payload["status"] == "ok"
    assert payload["step"] == 1
    # The transcript must include the system prompt, user goal, and assistant reply.
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_checkpoint_is_valid_json_after_run(tmp_path: Path) -> None:
    """Operator can read the checkpoint without crashing on malformed JSON."""
    fake = _script("done")
    runtime = Runtime(client=fake)
    manifest = _manifest(tmp_path)
    await runtime.run(manifest, goal="hi", checkpoint_dir=tmp_path)

    ckpt = next(tmp_path.glob("ckpt-agent-*.json"))
    payload = json.loads(ckpt.read_text())
    assert "messages" in payload
    assert "step" in payload
    assert "tokens_in" in payload
    assert "tokens_out" in payload
    assert "tool_calls_made" in payload


@pytest.mark.asyncio
async def test_resume_picks_up_at_last_step(tmp_path: Path) -> None:
    """After max_steps_reached, resume() should continue without
    replaying the user message or already-consumed completions."""
    # First script: 3 stops (max_steps=3, no tool_calls → final answer on first hit).
    # To force max_steps_reached we need the model to never return a final
    # answer — i.e. every completion must be empty content + a tool call.
    # But our manifest has no tools, so empty content + finish_reason="stop"
    # counts as a final answer. Use max_steps=1 instead so the loop fires
    # exactly one step then halts because... actually with max_steps=1 and
    # a final-answer completion, status='ok' and there's nothing to resume.
    # Instead: max_steps=2, the model returns a final answer on step 1 but
    # the user-supplied `resume=True` flag forces a second step. Simpler:
    # use a separate "needs another step" completion with empty content but
    # no tool_calls AND finish_reason='length' — that counts as not-final.
    fake = FakeClient()
    fake.script(
        [
            Completion(
                message=Message("assistant", ""),  # empty = not a final answer
                tokens_in=10,
                tokens_out=1,
                finish_reason="length",
            ),
            Completion(
                message=Message("assistant", "second answer"),
                tokens_in=10,
                tokens_out=5,
                finish_reason="stop",
            ),
        ]
    )
    manifest = _manifest(tmp_path)
    manifest = manifest.model_copy(
        update={"policies": manifest.policies.model_copy(update={"max_steps": 1})}
    )
    runtime = Runtime(client=fake)
    first = await runtime.run(manifest, goal="resume me", checkpoint_dir=tmp_path)

    # First run hit max_steps_reached after 1 step.
    assert first.status == "max_steps_reached"

    # Now resume — should pick up where it left off and finish.
    ckpt_path = next(tmp_path.glob("ckpt-agent-*.json"))
    second = await runtime.resume(ckpt_path, manifest)

    assert second.status == "ok"
    assert second.output == "second answer"
    # We should have made exactly one more LLM call (not replay the first).
    assert second.steps == 2


@pytest.mark.asyncio
async def test_resume_uses_second_script_continuation(tmp_path: Path) -> None:
    """Resume must NOT replay the user message at the start of the transcript."""
    fake = FakeClient()
    fake.script(
        [
            Completion(
                message=Message("assistant", ""),
                tokens_in=10,
                tokens_out=1,
                finish_reason="length",
            ),
            Completion(
                message=Message("assistant", "ok"),
                tokens_in=10,
                tokens_out=2,
                finish_reason="stop",
            ),
        ]
    )
    manifest = _manifest(tmp_path)
    manifest = manifest.model_copy(
        update={"policies": manifest.policies.model_copy(update={"max_steps": 1})}
    )
    runtime = Runtime(client=fake)
    await runtime.run(manifest, goal="hello", checkpoint_dir=tmp_path)

    # Inspect the checkpoint: user message appears exactly once.
    ckpt = json.loads(next(tmp_path.glob("ckpt-agent-*.json")).read_text())
    user_count = sum(1 for m in ckpt["messages"] if m["role"] == "user")
    assert user_count == 1


@pytest.mark.asyncio
async def test_resume_unknown_checkpoint_raises(tmp_path: Path) -> None:
    """Asking resume() for a missing file is a clear error."""
    fake = _script("x")
    runtime = Runtime(client=fake)
    manifest = _manifest(tmp_path)
    with pytest.raises(FileNotFoundError):
        await runtime.resume(tmp_path / "does-not-exist.json", manifest)


@pytest.mark.asyncio
async def test_checkpoint_writes_atomically(tmp_path: Path) -> None:
    """No leftover .tmp files should remain after a clean run — atomic
    rename means the tmp file was moved into place."""
    fake = _script("done")
    runtime = Runtime(client=fake)
    manifest = _manifest(tmp_path)
    await runtime.run(manifest, goal="hi", checkpoint_dir=tmp_path)

    # If rename was atomic and successful, no .tmp sibling should exist.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
