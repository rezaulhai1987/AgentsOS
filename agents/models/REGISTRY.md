# Free / Open-Weight LLM Registry

Models AgentsOS can target today via `provider: ollama` (default), `provider: openai`
(any OpenAI-compat server — llama.cpp, vLLM, LM Studio, Groq free tier,
OpenRouter free tier, etc.), or `provider: llama.cpp` (direct HTTP).

> All entries are open-weight (Apache 2.0 / MIT / DeepSeek License / Llama
> community license / Gemma terms). "Cost" column is **$0** for local Ollama
> runs and per-token for hosted free tiers — local Ollama is the
> recommended path so the `max_cost_usd` policy in `manifest.policies`
> stays meaningful.

Ranked by **fit-for-agents** (function-calling, instruction-following,
context, license, hardware). 1 = best default.

| # | Model | License | Best for | Why |
|---|-------|---------|----------|-----|
| 1 | **Llama 3.3 70B Instruct** | Llama 3 Community | Default production agent | Best-in-class tool use among open weights at the 70B tier; 128K context; quantised 4-bit fits in 24 GB VRAM (q4_K_M). The single best Ollama default for AgentsOS today. |
| 2 | **DeepSeek-V3 / DeepSeek-R1** | DeepSeek License (open weights, MIT-like) | Reasoning-heavy agents (planning, math, code review) | R1 matches o1-style chain-of-thought at open weights; V3 is the chat model. Both served via Ollama. Pair R1 with V3 in a router if you need both. |
| 3 | **Qwen 2.5 72B Instruct** | Apache 2.0 | Multilingual + tool use | Strongest tool-calling among the permissively-licensed models; 128K context; Apache 2.0 means **no use restrictions** unlike Llama's community license. Excellent Ollama support. |
| 4 | **Mistral Large 2 (123B)** | Mistral Research License (free for research / commercial under threshold) | Long-context agents | 128K context, strong function calling, good for codebase-scale tasks. Heavy — needs ≥48 GB VRAM or aggressive quantisation. |
| 5 | **Llama 3.1 8B Instruct** | Llama 3 Community | Fast local agent (developer laptop) | Fits 8 GB VRAM; 128K context; surprisingly strong tool use for the size. The right default when you can't run 70B. |
| 6 | **Phi-4 (14B)** | MIT | Small-but-capable reasoning | Microsoft's Phi-4 punches well above its size on reasoning benchmarks; 16K context; great for `manifest.policies.max_steps`-heavy loops where speed matters. |
| 7 | **Gemma 3 27B IT** | Gemma Terms (open weights, permissive use) | Safety-tuned agent loops | Google's 27B IT model; strong instruction following, 128K context, ships with proper system-prompt handling that survives multi-turn agent loops without drifting. |
| 8 | **Command-R (Cohere, 35B)** | CC-BY-NC (non-commercial) | RAG-style agents with citations | Built for retrieval-augmented generation with grounded citations — exactly what an agent's memory layer wants. NC license rules it out for commercial products but it shines for personal/dev use. |
| 9 | **Yi-1.5 34B Chat** | Apache 2.0 | Bilingual (EN/ZH) agents | Apache 2.0 + strong bilingual performance + solid function calling. Useful when your agent must read Chinese docs or reply to Chinese-speaking users. |
| 10 | **gpt-oss-20b / 120b** | Apache 2.0 | OpenAI-style tool calling on open weights | OpenAI's first open-weight release; designed to match `gpt-4` tool-call JSON shape — adapters written against `tools=[…]` Just Work without prompt hacking. 120B needs ≥80 GB; 20B is the desktop sweet spot. |

## Honourable mentions
- **Nemotron-4 340B** (NVIDIA, OpenRAIL) — too large for most local rigs but unbeatable on long-context benchmarks if you have the hardware.
- **Mixtral 8x22B** (Apache 2.0) — MoE architecture, fast for its size, good fallback if Llama 3.3 70B doesn't fit.
- **DeepSeek-Coder-V2 Lite (16B)** — specialised for code-edit agents; useful as a sub-agent in a router.

## How AgentsOS picks a model

AgentsOS does **not** pick. The manifest picks:

```yaml
model:
  provider: ollama          # default
  id: llama3.3:70b-instruct-q4_K_M
  temperature: 0.7
```

`provider: ollama` resolves to `http://localhost:11434/v1` and uses the
existing `OpenAICompatClient` — Ollama exposes a chat-completions
endpoint at `/v1/chat/completions` since v0.1.30. **You don't need a new
adapter for Ollama**; the manifest-level provider name is the only
configuration change.

## Bring your own model

If you have a model that isn't in this list, you have two paths:

1. **Serve it with Ollama** (`ollama pull <model>`), then point AgentsOS
   at `provider: ollama` and any `id:`. The OpenAI-compat adapter handles
   the wire format.
2. **Serve it via any OpenAI-compat server** (llama.cpp's `server`,
   vLLM, LM Studio, text-generation-webui). Set
   `AGENTSOS_BASE_URL=http://localhost:8080/v1` and `provider: openai`.

The `llama.cpp` provider name is reserved for the day we add a dedicated
adapter that talks to llama.cpp's native `/completion` endpoint (vs the
OpenAI shim). Same idea for `hf` — direct `transformers` integration is
the v0.5 milestone.

## Verdict: what to download first

If you have a single 24 GB GPU:

```bash
ollama pull llama3.3:70b-instruct-q4_K_M
ollama pull deepseek-r1:70b          # reasoning agent
ollama pull qwen2.5:72b-instruct-q4_K_M
```

If you're on a laptop (8–16 GB):

```bash
ollama pull llama3.1:8b-instruct
ollama pull phi4:14b
ollama pull gpt-oss:20b
```

That covers ~95% of agent workloads AgentsOS exists to run — and every
single model on this list is **$0 / free** at the point of use when run
locally.