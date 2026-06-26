# Token & Cost Reduction — Skeptical Research Notes

> No 98%-reduction snake oil. **50–70% combined reductions are realistic; 90% needs real quality loss.**

## 1. Anthropic Prompt Caching
- **Mechanism:** Manual — mark `cache_control: {type: "ephemeral"}` breakpoints.
- **TTL:** 5 min (free) or 1h (1.25× write, 2× read on top of base).
- **Savings:** **~90% on the cached portion.** Real hit rates 60–85% once warmed.
- **Quality:** Lossless. **Complexity:** Trivial — group static content (system prompt, tool docs, RAG) before breakpoints.
- **Ref:** docs.anthropic.com/en/docs/build-with-claude/prompt-caching

## 2. OpenAI Prompt Caching (Oct 2024)
- **Mechanism:** **Automatic** for prompts >1024 tokens; longest-prefix match.
- **TTL:** 5–10 min idle (up to 24h for some models, 2025).
- **Discount:** **50% off cached input.** Hit rates 40–70% (lower — no breakpoint control).
- **Quality:** Lossless. **Ref:** platform.openai.com/docs/guides/prompt-caching

## 3. Context Compression
| Method | Reduction | Quality Δ |
|---|---|---|
| Sliding window | 50–90% | Loses long-range facts |
| Rolling summarization | 60–80% | 5–15% recall drop on long-horizon QA |
| **LLMLingua** | 5–10× practical (20× claimed) | <3% ppl on paper; higher on Q&A |
| **LLMLingua-2 / Selective Context** | ~10× | Slightly better than v1 |
| **LongLLMLingua** | 4–8× | Often *outperforms* full context on long-doc QA |
| **RECOMP** (retrieve + compress) | 6–7× | Matches full context on QA |

>10× is a quality tax. Multi-hop reasoning degrades. No native provider support.

## 4. Token-Efficient Tool Schemas
A 50-tool JSON dump = **3–8k tokens per turn** — often the largest line item.
- **Retrieve top-K tools by query similarity:** **60–90% schema reduction.**
- **Trim descriptions** (adjectives, examples); tool-selection accuracy holds.
- **Reference docs lazily** in `description`; let the model ask.
- Send system prompt + tool list as the **cacheable prefix** (compounds with §1/§2).

## 5. Response Caching
- **Exact-match (Redis):** 20–60% hit rate; ~100% saved on hits.
- **Semantic cache (GPTCache, LangCache):** +10–30% over exact-match. **Risk:** false positives on similar-but-different questions. Use only when the answer is stable (FAQ, doc lookup).
- **Ref:** zilliz.com/gptcache

## 6. Model Routing / Cascading
- **FrugalGPT** (Stanford 2023, still relevant): **4× cost reduction** at <1% quality loss with a scorer+router.
- **RouteLLM** (2024): ~85% of GPT-4 quality at ~35% of cost.
- **NotDiamond / Martian / OpenRouter auto:** 40–70% cost reduction when 30–50% of traffic routes down.
- **Implementation:** small classifier or cheap LLM on the prompt. **Calibration > algorithm.**
- **Ref:** arxiv 2305.05176, 2406.18665

## 7. Context Pruning
- **Lost-in-the-middle** (Liu 2023): middle of long context recalled ~30% worse than start/end. **Pruning the middle of retrieved RAG context = 40–60% savings, no recall loss.**
- **Attention sinks / StreamingLLM** (2024): keep first 4 tokens + sliding window = unbounded context, bounded KV.
- **Ref:** arxiv 2307.03172, 2309.17453

## 8. "Compaction" (Claude Code / Cursor / Hermes pattern)
- When context nears its limit, the agent writes a **structured summary** (goals, decisions, file states, open questions, recent diffs). Older raw turns are discarded; the summary replaces them.
- **What survives:** semantic gist + last 1–3 turns verbatim + recent tool results.
- **What doesn't:** exact phrasing, intermediate reasoning, error tracebacks.
- **Preservation:** ~5–15% of original tokens, retaining **~80–90% of task-relevant info** (per Claude Code docs).
- Works for coding; degrades on verbatim-recall tasks (transcription, diff review).

## 9. tiktoken
- `o200k_base` (GPT-4o), `cl100k_base` (GPT-4/3.5), `p50k_base` (legacy).
- Each tool-call message = ~10–25 token overhead; batch results to amortize.
- Don't count cacheable prefixes against "active" cost. **Ref:** github.com/openai/tiktoken

## 10. 2024–2025 Research
- **H2O / Scissorhands KV eviction:** 60–80% KV cache reduction, <1% quality loss (arxiv 2306.14048, 2305.17118).
- **Speculative decoding:** **2–3× faster, zero quality change, zero token savings** — speed only.
- **Early exit (CALM):** ~20–30% compute savings on easy tokens.
- **Gemini context caching** (2024): explicit, 1h TTL, Anthropic-like pricing.

---

## What Actually Compounds

For a coding agent on Anthropic + OpenAI:

| Stack | Reduction | Quality | Notes |
|---|---|---|---|
| **1.** Anthropic caching + tool pruning | **50–60%** | Lossless | Do first. One afternoon. |
| **2.** + response cache + smart routing | **65–75%** | <2% loss | Add weeks; tune the router. |
| **3.** + compaction + middle-pruning | **80–85%** | 3–7% loss on hard tasks | Compaction is the real unlock. |
| **4.** + LLMLingua on RAG context | **85–90%** | 8–15% loss on multi-hop | Diminishing returns. |
| **5.** + local Llama-3.1-8B for trivial turns | **90–92%** | 10–20% loss if misrouted | Local handles ~40% of simple turns. |

**Honest ceiling:** **~90% sustained** is achievable for well-scoped agents. The last 10% to "98%" is a **quality cliff** — if a vendor promises 98%, ask which of (a) benchmark, (b) workload, (c) baseline they're hiding.

**The rule:** lossless techniques (caching, easy-task routing, schema pruning) **always run**. Lossy ones are per-feature — measure on *your* task first.
