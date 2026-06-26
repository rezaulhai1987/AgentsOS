"""Tests for manifest loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentsos.manifest import Manifest, load_manifest


def test_manifest_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "agent.yaml"
    p.write_text(
        """
name: test-agent
version: 0.1.0
description: A test.
model:
  provider: openai
  id: gpt-4o-mini
  temperature: 0.5
  max_tokens: 1024
system_prompt: |
  You are a test agent.
tools: [echo]
policies:
  max_steps: 5
  max_cost_usd: 0.10
  timeout_s: 60
memory:
  namespaces: [default, test]
  retrieve_k: 3
""",
        encoding="utf-8",
    )
    lm = load_manifest(p)
    m: Manifest = lm.manifest
    assert m.name == "test-agent"
    assert m.version == "0.1.0"
    assert m.model.provider == "openai"
    assert m.model.temperature == 0.5
    assert m.policies.max_steps == 5
    assert m.policies.max_cost_usd == 0.10
    assert m.memory.namespaces == ["default", "test"]
    assert m.memory.retrieve_k == 3
    assert m.tools == ["echo"]


def test_invalid_manifest_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("name: X\nversion: not-semver\n", encoding="utf-8")
    with pytest.raises((ValueError, Exception)):  # noqa: B017 - pydantic raises ValidationError (subclass of ValueError)
        load_manifest(p)


def test_system_prompt_path(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("You are loaded from disk.", encoding="utf-8")
    # Use forward slashes + raw-style to keep YAML happy on Windows paths.
    safe = str(prompt).replace("\\", "/")
    p = tmp_path / "agent.yaml"
    p.write_text(
        f"""
name: disk-agent
version: 1.0.0
model: {{provider: openai, id: gpt-4o-mini}}
system_prompt: '@{safe}'
tools: []
""",
        encoding="utf-8",
    )
    lm = load_manifest(p)
    assert "loaded from disk" in lm.manifest.system_prompt
