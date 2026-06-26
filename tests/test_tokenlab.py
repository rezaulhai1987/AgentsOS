"""Tests for the tokenlab toolkit.

The numbers in these tests are the real measured outputs from a
synthetic long-running agent transcript. We pin them so a tokenizer
swap can't silently break our budget assumptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenlab import budget, cache, compress, count, router, schema, trim


def test_count_basic() -> None:
    assert count.count("hello") == 1
    assert count.count("hello world") == 2
    assert count.count("") == 0
    assert count.estimate("a" * 100) >= 25


def test_count_messages_overhead() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    out = count.count_messages(msgs)
    assert len(out) == 1
    # +4 overhead for the message envelope
    assert out[0].tokens >= 5
    assert count.total(msgs) == out[0].tokens


def test_budget_charges_and_blocks() -> None:
    b = budget.Budget(per_call=10, per_session=20, per_session_cost_usd=1.0)
    b.charge([{"role": "user", "content": "small"}], cost_usd=0.10)
    assert b.used_session > 0
    with pytest.raises(budget.BudgetExceeded):
        b.charge([{"role": "user", "content": "x" * 200}])
    with pytest.raises(budget.BudgetExceeded):
        b.charge([{"role": "user", "content": "tiny"}], cost_usd=2.0)


def test_compress_skips_short_transcripts() -> None:
    c = compress.Compressor(keep_last=4, trigger_at=10_000)
    msgs = [{"role": "user", "content": "hi"}] * 3
    out = c.compress(msgs)
    assert out == msgs


def test_compress_summarizes_middle() -> None:
    c = compress.Compressor(keep_last=2, trigger_at=10)
    msgs = (
        [{"role": "user", "content": "x" * 100}]
        + [{"role": "user", "content": f"line {i}\n"} for i in range(40)]
        + [{"role": "user", "content": "final"}]
    )
    out = c.compress(msgs)
    # The middle is summarized; the last 2 messages survive verbatim.
    assert out[-1]["content"] == "final"
    assert any("summary of" in m.get("content", "") for m in out)
    s = c.stats(msgs, out)
    assert s["tokens_saved"] > 0
    assert 0.0 < s["ratio"] < 1.0


def test_compress_is_cached_across_calls() -> None:
    c = compress.Compressor(keep_last=2, trigger_at=10)
    msgs = [{"role": "user", "content": "x" * 100}] + [
        {"role": "user", "content": f"line {i}"} for i in range(40)
    ]
    a = c.compress(msgs)
    b = c.compress(msgs)
    # Same span -> cached summary -> identical anchor content.
    anchor_a = next(m for m in a if "summary" in m["content"])
    anchor_b = next(m for m in b if "summary" in m["content"])
    assert anchor_a == anchor_b


def test_truncate_tool_messages_writes_to_disk(tmp_path: Path) -> None:
    big = "x = 1\n" * 5_000
    msgs = [{"role": "tool", "content": big}]
    spill = tmp_path / "spill"
    out = trim.truncate_tool_messages(msgs, max_bytes=2_000, spill_dir=spill)
    assert len(out) == 1
    assert "elided" in out[0]["content"]
    assert (spill / "tool_0.txt").exists()
    assert (spill / "tool_0.txt").stat().st_size > 2_000


def test_truncate_passthrough_for_small_input() -> None:
    msgs = [{"role": "tool", "content": "small"}]
    out = trim.truncate_tool_messages(msgs, max_bytes=1_000)
    assert out[0]["content"] == "small"


def test_schema_minimize_drops_noise() -> None:
    s = {
        "type": "object",
        "title": "Input",
        "properties": {
            "path": {"type": "string", "default": "/tmp", "description": "p"},
            "count": {"type": "integer", "examples": [1]},
        },
        "additionalProperties": False,
    }
    out = schema.minimize(s)
    assert "default" not in str(out)
    assert "examples" not in str(out)
    assert "title" not in str(out)
    # "p" was below the 12-char description threshold and was dropped.
    assert "path" in out["properties"]
    assert "description" not in out["properties"]["path"]


def test_schema_minimize_tool_list_shrinks() -> None:
    tools = [
        {
            "name": f"t{i}",
            "description": f"Tool {i} " * 30,
            "input_schema": {
                "type": "object",
                "title": "X",
                "properties": {
                    "a": {"type": "string", "default": "x", "description": "short"},
                },
                "additionalProperties": False,
            },
        }
        for i in range(20)
    ]
    big = schema.schema_size(tools)
    small = schema.schema_size(schema.minimize_tool_list(tools))
    assert small < big
    assert (1 - small / big) >= 0.10  # at least 10% smaller


def test_response_cache_exact_hit(tmp_path: Path) -> None:
    c = cache.ResponseCache(root=tmp_path / "c")
    key = cache.request_key("m", [{"role": "user", "content": "hi"}])
    assert c.get_exact(key) is None
    c.put(key, {"ok": True})
    assert c.get_exact(key) == {"ok": True}
    s = c.stats()
    assert s["exact_entries"] == 1
    assert s["total_hits"] >= 1


def test_response_cache_survives_restart(tmp_path: Path) -> None:
    root = tmp_path / "c"
    c1 = cache.ResponseCache(root=root)
    key = cache.request_key("m", [{"role": "user", "content": "hi"}])
    c1.put(key, {"answer": 42})
    c2 = cache.ResponseCache(root=root)
    assert c2.get_exact(key) == {"answer": 42}


def test_router_default_routes_exist() -> None:
    r = router.Router()
    route = r.route(task="classify")
    assert route.primary.name == "claude-haiku-4-5"
    assert route.fallback.name == "claude-sonnet-4"
    # Synthesize should default to sonnet primary, opus fallback.
    synth = r.route(task="synthesize")
    assert synth.primary.name == "claude-sonnet-4"
    assert synth.fallback.name == "claude-opus-4"


def test_router_force_overrides() -> None:
    r = router.Router()
    router.force(r, "haiku")
    assert r.route(task="synthesize").primary.name == "claude-haiku-4-5"


def test_router_estimate_cost() -> None:
    r = router.Router()
    route = r.route(task="classify")
    cost = r.estimate_cost(route, tokens_in=1000, tokens_out=500)
    # 1000 in @ $0.001/1k + 500 out @ $0.005/1k = $0.001 + $0.0025 = $0.0035
    assert abs(cost - 0.0035) < 1e-9


def test_cli_demo_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo command must execute end-to-end without errors."""
    monkeypatch.chdir(tmp_path)
    from typer.testing import CliRunner

    from tokenlab.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0
    assert "TOTAL reduction" in result.stdout
