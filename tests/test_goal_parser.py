"""Tests for the v0.3.10 /goal parser and registry binding."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentsos.goal_parser import (
    GoalOp,
    ParseError,
    apply_goal,
    parse_goal,
    run_goal_command,
)
from agentsos.work_registry import Registry, TaskStatus


@pytest.fixture()
def reg(tmp_path: Path) -> Registry:
    return Registry(path=tmp_path / "reg.json")


def _all_tasks(reg: Registry) -> list:
    """Helper: pull all tasks out as a list (snapshot.tasks is a dict)."""
    return list(reg.snapshot().tasks.values())


def _first_id(reg: Registry) -> str:
    tasks = _all_tasks(reg)
    assert tasks, "expected at least one task"
    return tasks[0].id


# ---- pure parser ----------------------------------------------------


def test_parse_list_default() -> None:
    op = parse_goal([])
    assert op.op == "list"
    assert op.limit == 25


def test_parse_list_with_filters() -> None:
    op = parse_goal(["list", "--status", "pending", "--limit", "5"])
    assert op.op == "list"
    assert op.status_filter == "pending"
    assert op.limit == 5


def test_parse_ls_alias() -> None:
    op = parse_goal(["ls"])
    assert op.op == "list"


def test_parse_add_minimal() -> None:
    op = parse_goal(["add", "Ship", "v0.4"])
    assert op.op == "add"
    assert op.title == "Ship v0.4"


def test_parse_add_with_budget_and_branch() -> None:
    op = parse_goal(
        [
            "add",
            "Ship",
            "v0.4",
            "--budget",
            "5.00",
            "--branch",
            "feat/x",
            "--deps",
            "a,b",
            "--notes",
            "tight",
        ]
    )
    assert op.budget_usd == 5.0
    assert op.branch == "feat/x"
    assert op.deps == ["a", "b"]
    assert op.notes == "tight"


def test_parse_start_done_fail_remove() -> None:
    for v in ("start", "done", "fail", "remove"):
        op = parse_goal([v, "abc12345"])
        assert op.op == v
        assert op.target_prefix == "abc12345"


def test_parse_note_via_flag() -> None:
    op = parse_goal(["note", "abc12345", "--notes", "needs review"])
    assert op.op == "note"
    assert op.target_prefix == "abc12345"
    assert op.notes == "needs review"


def test_parse_unknown_verb_raises() -> None:
    with pytest.raises(ParseError):
        parse_goal(["bogus"])


def test_parse_missing_target_raises() -> None:
    with pytest.raises(ParseError):
        parse_goal(["done"])


def test_parse_unknown_flag_raises() -> None:
    with pytest.raises(ParseError):
        parse_goal(["add", "title", "--bogus", "x"])


# ---- apply against registry -----------------------------------------


def test_add_creates_pending_task(reg: Registry) -> None:
    op = parse_goal(["add", "Ship", "v0.4", "--budget", "1.50"])
    res = apply_goal(reg, op)
    assert res.ok is True
    assert res.created is not None
    assert res.created.title == "Ship v0.4"
    assert res.created.status == TaskStatus.PENDING.value
    assert res.created.metadata.get("cost_budget_usd") == 1.5
    # round-trip
    again = _all_tasks(reg)[0]
    assert again.id == res.created.id


def test_list_returns_tasks(reg: Registry) -> None:
    apply_goal(reg, parse_goal(["add", "one"]))
    apply_goal(reg, parse_goal(["add", "two"]))
    res = apply_goal(reg, parse_goal(["list"]))
    assert res.ok is True
    assert "2 goal(s)" in res.text


def test_list_filter_by_status(reg: Registry) -> None:
    apply_goal(reg, parse_goal(["add", "a"]))
    apply_goal(reg, parse_goal(["add", "b"]))
    first_id = _first_id(reg)
    apply_goal(reg, parse_goal(["start", first_id[:14]]))
    res = apply_goal(reg, parse_goal(["list", "--status", "in_progress"]))
    assert "1 goal(s)" in res.text


def test_start_done_fail_note(reg: Registry) -> None:
    t = apply_goal(reg, parse_goal(["add", "ship"])).created
    assert t is not None
    pid = t.id[:14]
    res = apply_goal(reg, parse_goal(["start", pid]))
    assert res.ok is True
    assert _all_tasks(reg)[0].status == TaskStatus.IN_PROGRESS.value
    res = apply_goal(reg, parse_goal(["done", pid, "--notes", "merged!"]))
    assert res.ok is True
    assert _all_tasks(reg)[0].status == TaskStatus.DONE.value
    assert "merged!" in _all_tasks(reg)[0].notes


def test_remove(reg: Registry) -> None:
    t = apply_goal(reg, parse_goal(["add", "ship"])).created
    pid = t.id[:14]
    res = apply_goal(reg, parse_goal(["remove", pid]))
    assert res.ok is True
    assert _all_tasks(reg) == []


def test_ambiguous_prefix(reg: Registry) -> None:
    apply_goal(reg, parse_goal(["add", "alpha"]))
    apply_goal(reg, parse_goal(["add", "beta"]))
    # Both tasks start with the timestamp prefix
    first_id = _first_id(reg)
    res = apply_goal(reg, parse_goal(["done", first_id[:2]]))
    assert res.ok is False
    assert "ambiguous" in res.text


def test_no_match(reg: Registry) -> None:
    res = apply_goal(reg, parse_goal(["done", "nope-not-here"]))
    assert res.ok is False
    assert "no task" in res.text


# ---- top-level dispatcher -------------------------------------------


def test_run_goal_command_add_and_list(reg: Registry) -> None:
    reply = run_goal_command(reg, 'goal add "Ship v0.4" --budget 2.00 --branch feat/x')
    assert "created" in reply, reply
    reply = run_goal_command(reg, "goal list")
    assert "1 goal(s)" in reply
    assert "Ship v0.4" in reply


def test_run_goal_command_handles_bare_goal(reg: Registry) -> None:
    reply = run_goal_command(reg, "goal")
    assert "no goals" in reply


def test_run_goal_command_parse_error_is_friendly() -> None:
    reply = run_goal_command(reg, "goal done")
    assert "⚠️" in reply
    assert "id" in reply.lower()


def test_run_goal_command_quoted_title_with_special_chars(reg: Registry) -> None:
    reply = run_goal_command(reg, "goal add \"Ship v$0.4 (next)\"")
    assert "created" in reply


def test_run_goal_command_unknown_verb(reg: Registry) -> None:
    reply = run_goal_command(reg, "goal bogus")
    assert "⚠️" in reply
    assert "unknown subcommand" in reply