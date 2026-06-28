"""v0.3.10: Telegram `/goal` command parser.

Turns slash-command strings into structured operations against the
existing `agentsos.work_registry.Registry`. Designed to be called from
`TelegramBot.on_command` and `agents goal ...` CLI.

Grammar (single line):

    /goal add "<title>" [--budget <usd>] [--branch <name>] [--deps a,b,c] [--notes "<text>"]
    /goal list [--status pending|in_progress|done|failed|blocked] [--limit N]
    /goal start <id-prefix>
    /goal done <id-prefix> [--notes "<text>"]
    /goal fail <id-prefix> [--notes "<text>"]
    /goal remove <id-prefix>
    /goal note <id-prefix> "<text>"

Quoted strings keep their literal contents; shell-style token splitting is
NOT done — this parser is straight text-in / str-out. The dispatcher
already pre-splits on whitespace.

Why a separate module:
    * pure, testable in isolation
    * the same parser is reused by Telegram (`/goal ...`) and the CLI
      (`agents goal ...`)
    * it keeps bot.py and app.py free of a third grammar
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentsos.work_registry import Registry, Task, TaskStatus

# ------- operation model ---------------------------------------------


@dataclass
class GoalOp:
    """A single parsed command, ready to apply against a Registry."""

    op: str  # "add" | "list" | "start" | "done" | "fail" | "remove" | "note"
    title: str = ""
    budget_usd: float | None = None
    branch: str = ""
    deps: list[str] = field(default_factory=list)
    notes: str = ""
    status_filter: str = ""
    limit: int = 25
    target_prefix: str = ""  # id-prefix for start/done/fail/remove/note


@dataclass
class GoalResult:
    ok: bool
    op: str
    text: str
    created: Task | None = None
    affected: list[Task] = field(default_factory=list)


# ------- tokenizer / parser -----------------------------------------


# Pull the head noun from "/goal add ..." or "goal add ..." (the bot
# strips the leading slash before calling us).
def _verb(args: list[str]) -> str:
    return args[0].lower() if args else "list"


_VERBS = {"add", "list", "start", "done", "fail", "remove", "note", "ls"}


@dataclass
class ParseError(Exception):
    msg: str


def _next_required(args: list[str], i: int, name: str) -> tuple[str, int]:
    if i >= len(args):
        raise ParseError(f"missing required argument: {name}")
    return args[i], i + 1


def parse_goal(args: list[str]) -> GoalOp:
    """Parse argv (no leading /goal) into a GoalOp."""
    if not args:
        return GoalOp(op="list", limit=25)
    verb = _verb(args)
    if verb not in _VERBS:
        raise ParseError(f"unknown subcommand: {verb!r} (expected: {', '.join(sorted(_VERBS))})")
    rest = args[1:]
    if verb == "ls":
        verb = "list"

    op = GoalOp(op=verb)
    if verb == "list":
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "--status":
                v, i = _next_required(rest, i + 1, "--status")
                op.status_filter = v
            elif tok == "--limit":
                v, i = _next_required(rest, i + 1, "--limit")
                try:
                    op.limit = int(v)
                except ValueError as e:
                    raise ParseError(f"--limit expects integer, got {v!r}") from e
            else:
                raise ParseError(f"unexpected token for `list`: {tok!r}")
        return op

    if verb == "add":
        if not rest:
            raise ParseError("`add` needs a title: /goal add \"Ship v0.4\"")
        # Title may be multiple whitespace-separated tokens; collect
        # everything until the first `--flag`.
        title_parts: list[str] = []
        i = 0
        while i < len(rest) and not rest[i].startswith("--"):
            title_parts.append(rest[i])
            i += 1
        if not title_parts:
            raise ParseError("`add` needs a title: /goal add \"Ship v0.4\"")
        op.title = " ".join(title_parts)
        while i < len(rest):
            tok = rest[i]
            if tok == "--budget":
                v, i = _next_required(rest, i + 1, "--budget")
                try:
                    op.budget_usd = float(v)
                except ValueError as e:
                    raise ParseError(f"--budget expects number, got {v!r}") from e
            elif tok == "--branch":
                v, i = _next_required(rest, i + 1, "--branch")
                op.branch = v
            elif tok == "--deps":
                v, i = _next_required(rest, i + 1, "--deps")
                op.deps = [d.strip() for d in v.split(",") if d.strip()]
            elif tok == "--notes":
                v, i = _next_required(rest, i + 1, "--notes")
                op.notes = v
            else:
                raise ParseError(f"unexpected token for `add`: {tok!r}")
        return op

    # start / done / fail / remove / note — all need an id-prefix first.
    if not rest:
        raise ParseError(f"`{verb}` needs an id prefix (8 chars is plenty)")
    op.target_prefix = rest[0]
    i = 1
    while i < len(rest):
        tok = rest[i]
        if tok == "--notes":
            v, i = _next_required(rest, i + 1, "--notes")
            op.notes = v
        else:
            raise ParseError(f"unexpected token for `{verb}`: {tok!r}")
    return op


# ------- registry application ---------------------------------------


def _match_tasks(reg: Registry, prefix: str, status_filter: str = "") -> list[Task]:
    """Find tasks by id-prefix (case-insensitive) and optional status."""
    needle = prefix.lower()
    matches = [t for t in reg.snapshot().tasks.values() if t.id.lower().startswith(needle)]
    if status_filter:
        matches = [t for t in matches if t.status == status_filter]
    return matches


def _new_id(title: str) -> str:
    """Stable-ish id: ts-millis + slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24]
    ts = datetime.now(UTC).strftime("%y%m%d%H%M%S%f")[:14]
    return f"{ts}-{slug}" if slug else ts


def apply_goal(reg: Registry, op: GoalOp) -> GoalResult:
    """Apply a parsed GoalOp against `reg`. Returns a GoalResult.

    The registry is mutated in-place and `flush()`-ed.
    """
    if op.op == "list":
        tasks_dict = reg.snapshot().tasks
        rows: list[Task] = list(tasks_dict.values())
        if op.status_filter:
            rows = [t for t in rows if t.status == op.status_filter]
        rows.sort(key=lambda t: t.started_at, reverse=True)
        rows = rows[: max(1, op.limit)]
        if not rows:
            return GoalResult(True, "list", "no goals match — add one with `/goal add \"...\"`")
        lines = [f"{len(rows)} goal(s)"]
        for t in rows:
            lines.append(
                f"  {t.id[:14]}  [{t.status:<11}] {t.title}"
            )
        return GoalResult(True, "list", "\n".join(lines), affected=rows)

    if op.op == "add":
        if not op.title.strip():
            raise ParseError("title is required")
        tid = _new_id(op.title)
        task = Task(
            id=tid,
            title=op.title,
            status=TaskStatus.PENDING.value,
            branch=op.branch,
            notes=op.notes,
            deps=list(op.deps),
            metadata=({"cost_budget_usd": op.budget_usd} if op.budget_usd is not None else {}),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        reg.upsert_task(task)
        reg.flush()
        return GoalResult(True, "add", f"created `{task.id}` — {task.title}", created=task)

    # start / done / fail / remove / note all need a matching task
    matches = _match_tasks(reg, op.target_prefix)
    if not matches:
        return GoalResult(False, op.op, f"no task matches prefix {op.target_prefix!r}")
    if len(matches) > 1:
        ids = ", ".join(t.id[:14] for t in matches[:5])
        return GoalResult(False, op.op, f"ambiguous prefix {op.target_prefix!r}: {ids}")
    task = matches[0]
    if op.op == "start":
        reg.mark_status(task.id, TaskStatus.IN_PROGRESS.value, notes=op.notes)
    elif op.op == "done":
        reg.mark_status(task.id, TaskStatus.DONE.value, notes=op.notes)
    elif op.op == "fail":
        reg.mark_status(task.id, TaskStatus.FAILED.value, notes=op.notes)
    elif op.op == "remove":
        # Remove isn't on Registry directly; emulate by writing a fresh
        # snapshot without this task. Registry.flush() will persist.
        snap = reg.snapshot()
        snap.tasks.pop(task.id, None)
        reg.flush()
        return GoalResult(True, "remove", f"removed `{task.id}` — {task.title}", affected=[task])
    elif op.op == "note":
        reg.upsert_task(
            Task(
                id=task.id,
                title=task.title,
                status=task.status,
                phase=task.phase,
                branch=task.branch,
                pr_url=task.pr_url,
                last_commit=task.last_commit,
                last_test_run=task.last_test_run,
                tests_total=task.tests_total,
                tests_passed=task.tests_passed,
                started_at=task.started_at,
                finished_at=task.finished_at,
                notes=(task.notes + ("\n" if task.notes else "") + op.notes).strip(),
                deps=list(task.deps),
                metadata=dict(task.metadata),
            )
        )
    else:  # pragma: no cover  (defensive)
        raise ParseError(f"unsupported op: {op.op!r}")
    reg.flush()
    # Pull the refreshed task back out
    refreshed = _match_tasks(reg, task.id[:14])
    affected = refreshed if refreshed else [task]
    return GoalResult(True, op.op, f"{op.op} ok — `{task.id}` {task.title}", affected=affected)


# ------- dispatcher shim used by both Telegram and CLI ----------------


def run_goal_command(reg: Registry, text: str) -> str:
    """Top-level: take the full text after `/goal` and return a reply.

    Used by Telegram's `on_command("goal", argv)` and by the CLI.
    """
    # shlex lets users type quoted titles; preserves `$` etc.
    try:
        argv = shlex.split(text.strip())
    except ValueError as e:
        return f"parse error: {e}"
    # argv starts with the verb. If user wrote `/goal` with nothing after,
    # default to `list`.
    if argv and argv[0].startswith("goal"):
        argv = argv[1:]
    try:
        op = parse_goal(argv)
    except ParseError as e:
        return f"⚠️ {e.msg}\nusage: /goal add|list|start|done|fail|remove|note ..."
    try:
        result = apply_goal(reg, op)
    except ParseError as e:
        return f"⚠️ {e.msg}"
    return result.text