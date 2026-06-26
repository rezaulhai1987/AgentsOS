"""Response cache.

Two layers, cheapest first:
  1. exact-match: hash of (model, system, messages, tools) -> response
  2. semantic (optional): embed the last user turn, find nearest neighbor
     in a small vector store, accept if cosine > threshold

Layer 1 is free, perfectly safe for deterministic prompts (classifiers,
templated code generation, JSON extraction). Layer 2 needs an embedding
function; we accept any callable so callers can plug in a local model
(via llama-cpp) or a remote one.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .count import count

Embedder = Callable[[str], list[float]]


def _hash_request(model: str, messages: list[dict], tools: list[dict] | None) -> str:
    payload = {
        "m": model,
        "msg": [{"r": m.get("role", ""), "c": m.get("content", "")} for m in messages],
        "t": tools or [],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class CacheEntry:
    key: str
    response: Any
    created: float
    hits: int = 0


class ResponseCache:
    """Two-tier response cache: exact + semantic.

    The cache is file-backed so a long-running agent loop can survive
    restarts. For semantic lookup we use a tiny in-memory vector index;
    swap with a real store for production.
    """

    def __init__(
        self,
        root: Path | None = None,
        embedder: Embedder | None = None,
        semantic_threshold: float = 0.92,
    ) -> None:
        self.root = Path(root) if root else None
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.semantic_threshold = semantic_threshold
        self._exact: dict[str, CacheEntry] = {}
        self._vectors: list[tuple[str, list[float]]] = []  # (key, embedding)
        self._persist: list[CacheEntry] = []

    def _file_for(self, key: str) -> Path | None:
        if not self.root:
            return None
        return self.root / f"{key}.json"

    def get_exact(self, key: str) -> Any | None:
        e = self._exact.get(key)
        if e:
            e.hits += 1
            return e.response
        f = self._file_for(key)
        if f and f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            e = CacheEntry(
                key=key,
                response=data["response"],
                created=data["created"],
                hits=1,
            )
            self._exact[key] = e
            return e.response
        return None

    def put(self, key: str, response: Any) -> None:
        e = CacheEntry(key=key, response=response, created=time.time())
        self._exact[key] = e
        f = self._file_for(key)
        if f:
            f.write_text(
                json.dumps({"response": response, "created": e.created}),
                encoding="utf-8",
            )

    def get_semantic(self, query: str) -> Any | None:
        if not self.embedder or not self._vectors:
            return None
        q = self.embedder(query)
        best_key = None
        best_score = -1.0
        for k, v in self._vectors:
            s = _cosine(q, v)
            if s > best_score:
                best_score = s
                best_key = k
        if best_key and best_score >= self.semantic_threshold:
            entry = self._exact.get(best_key)
            if entry:
                entry.hits += 1
                return entry.response
        return None

    def index_semantic(self, key: str, text: str) -> None:
        if not self.embedder:
            return
        self._vectors.append((key, self.embedder(text)))

    def stats(self) -> dict[str, Any]:
        total_hits = sum(e.hits for e in self._exact.values())
        return {
            "exact_entries": len(self._exact),
            "semantic_vectors": len(self._vectors),
            "total_hits": total_hits,
        }


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def request_key(model: str, messages: list[dict], tools: list[dict] | None = None) -> str:
    """Public helper — used by callers to get the exact-match key."""
    return _hash_request(model, messages, tools)


def estimated_savings(hit_text: str, cost_per_1k: float = 0.003) -> float:
    """Estimate USD saved by a cache hit (Anthropic Sonnet input price)."""
    return (count(hit_text) / 1000) * cost_per_1k
