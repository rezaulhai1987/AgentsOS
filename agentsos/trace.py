"""Structured trace events emitted by the runtime."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class TraceEvent:
    agent: str
    step: int
    kind: Literal["step.started", "llm.called", "tool.called", "step.completed", "error"]
    started: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class JsonlTraceSink:
    """Append-only trace writer. Default location: ./traces/<agent>.jsonl"""

    def __init__(self, root: str | Path = "traces") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def emit(self, ev: TraceEvent) -> None:
        path = self.root / f"{ev.agent}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(ev.to_jsonl() + "\n")
