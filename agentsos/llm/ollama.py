"""Ollama adapter.

Ollama exposes an OpenAI-compatible chat-completions endpoint at
``http://localhost:11434/v1/chat/completions`` (since Ollama v0.1.30) and
accepts any model tag you've pulled with ``ollama pull <name>``.

This module is a thin subclass of :class:`OpenAICompatClient` that locks
in Ollama's defaults — base URL and "no API key required" — so a manifest
saying ``provider: ollama`` works out of the box. If the user wants a
non-default Ollama URL (remote host, custom port, Ollama behind a
reverse proxy), they pass ``model.base_url`` in the manifest and the
constructor honours it.

If you have an existing ``OpenAICompatClient`` pointed at some other
provider, no changes are needed — Ollama follows the same wire format.
This class exists purely so the manifest's ``provider: ollama`` lookup
hits a registered adapter.
"""

from __future__ import annotations

import os

import httpx

from .openai_compat import OpenAICompatClient, register_client

_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"  # read lazily in __init__


@register_client("ollama")
class OllamaClient(OpenAICompatClient):
    """Calls Ollama's OpenAI-compat endpoint.

    No API key is sent — Ollama's local server doesn't require one. If
    you've put Ollama behind a proxy that does, set the
    ``AGENTSOS_OLLAMA_BASE_URL`` env var or pass ``base_url`` in the
    manifest.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            provider="ollama",
            base_url=base_url or os.environ.get("AGENTSOS_OLLAMA_BASE_URL", _DEFAULT_OLLAMA_URL),
            # Ollama doesn't require an API key; an empty string is fine
            # because the parent class only sets the Authorization header
            # when `api_key` is truthy.
            api_key=api_key if api_key is not None else "",
            timeout_s=timeout_s,
            http=http,
        )
