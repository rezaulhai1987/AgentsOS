"""Manifest loading and validation for AgentsOS agents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ModelSpec(BaseModel):
    # `fake` is the test escape hatch — never valid in a real agent YAML.
    provider: str = Field(pattern="^(openai|anthropic|ollama|llama.cpp|hf|fake)$")
    base_url: str | None = None  # per-agent override; falls back to provider default
    api_key: str | None = None  # per-agent override; falls back to env var
    id: str
    temperature: float = 0.7
    max_tokens: int = 4096


class Policies(BaseModel):
    max_steps: int = 25
    max_cost_usd: float = 1.00
    timeout_s: int = 600


class MemorySpec(BaseModel):
    namespaces: list[str] = Field(default_factory=lambda: ["default"])
    retrieve_k: int = 5


class Manifest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = ""
    model: ModelSpec
    system_prompt: str
    tools: list[str] = Field(default_factory=list)
    policies: Policies = Field(default_factory=Policies)
    memory: MemorySpec = Field(default_factory=MemorySpec)

    @field_validator("system_prompt")
    @classmethod
    def resolve_prompt_path(cls, v: str) -> str:
        # If system_prompt starts with "@", treat the rest as a path relative to CWD.
        if v.startswith("@"):
            return Path(v[1:]).read_text(encoding="utf-8")
        return v


@dataclass(frozen=True)
class LoadedManifest:
    manifest: Manifest
    source: Path

    @property
    def id(self) -> str:
        return f"{self.manifest.name}@{self.manifest.version}"


def load_manifest(path: str | Path) -> LoadedManifest:
    """Load a YAML manifest from disk and validate it."""
    p = Path(path)
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))
    m = Manifest(**raw)
    return LoadedManifest(manifest=m, source=p.resolve())


def iter_templates(roots: list[Path]) -> list[LoadedManifest]:
    """Discover all YAML templates under the given roots."""
    out: list[LoadedManifest] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.yaml")):
            try:
                out.append(load_manifest(p))
            except Exception as e:  # noqa: BLE001 — surface in caller
                raise RuntimeError(f"Invalid manifest at {p}: {e}") from e
    return out
