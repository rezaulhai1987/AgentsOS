"""Operator CLI — the `agents` command.

Currently exposes:
- `agents list-templates`
- `agents run --template <name> --goal "<text>"`
- `agents validate <path-to-manifest>`
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ...manifest import load_manifest
from ...registry import ManifestRegistry, ToolRegistry
from ...runtime import Runtime
from ...tools_builtin import BUILTINS

app = typer.Typer(add_completion=False, help="AgentsOS operator CLI")
console = Console()


def _default_template_dirs() -> list[Path]:
    # Walk up from CWD looking for an `agents/templates` dir.
    cwd = Path.cwd().resolve()
    candidates = [
        cwd / "agents" / "templates",
        cwd.parent / "agents" / "templates",
        cwd.parent.parent / "agents" / "templates",
    ]
    return [p for p in candidates if p.exists()]


def _build_registry() -> ManifestRegistry:
    return ManifestRegistry.from_template_dirs(_default_template_dirs())


def _build_tool_registry() -> ToolRegistry:
    r = ToolRegistry()
    for name, fn in BUILTINS.items():
        r.register(name, fn)
    return r


@app.command("list-templates")
def list_templates() -> None:
    """List all agent templates discoverable from the current directory."""
    reg = _build_registry()
    table = Table(title="Agent templates", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("version")
    table.add_column("model")
    table.add_column("tools")
    for lm in reg.all():
        m = lm.manifest
        table.add_row(
            m.name,
            m.version,
            f"{m.model.provider}/{m.model.id}",
            ", ".join(m.tools) or "—",
        )
    if not reg.all():
        console.print(
            "[yellow]No templates found. Run from a directory with agents/templates/.[/yellow]"
        )
    else:
        console.print(table)


@app.command("validate")
def validate(path: str) -> None:
    """Validate a single manifest file."""
    try:
        lm = load_manifest(path)
    except Exception as e:
        console.print(f"[red]INVALID[/red] {path}: {e}")
        raise typer.Exit(code=1) from None
    console.print(f"[green]OK[/green] {lm.id}  ({lm.source})")


@app.command("run")
def run(
    template: str = typer.Option(..., "--template", "-t", help="Template name (without version)"),
    goal: str = typer.Option(..., "--goal", "-g", help="Goal / task description"),
    sandbox: str = typer.Option("process", help="Runtime sandbox backend"),
) -> None:
    """Spawn an agent from a template and run it on a goal."""
    import asyncio

    reg = _build_registry()
    matches = [lm for lm in reg.all() if lm.manifest.name == template]
    if not matches:
        console.print(f"[red]No template named[/red] {template!r}. Run `agents list-templates`.")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        versions = ", ".join(lm.manifest.version for lm in matches)
        console.print(
            f"[red]Multiple versions:[/red] {versions}. Disambiguate by editing template."
        )
        raise typer.Exit(code=1)
    lm = matches[0]
    rt = Runtime(sandbox=sandbox)
    result = asyncio.run(rt.run(lm.manifest, goal))
    console.print(f"[bold]{result.agent}[/bold]  status={result.status}  steps={result.steps}")
    console.print(result.output)


@app.command("tools")
def tools() -> None:
    """List built-in tools available to agents."""
    r = _build_tool_registry()
    table = Table(title="Built-in tools", show_header=True)
    table.add_column("name")
    table.add_column("doc")
    for name in r.names():
        fn = r.get(name)
        doc = (fn.__doc__ or "").splitlines()[0]
        table.add_row(name, doc)
    console.print(table)


# --- daemon control (v0.3.8) -----------------------------------------------

daemon_app = typer.Typer(help="Daemon lifecycle (start/pause/resume/stop/status)")
app.add_typer(daemon_app, name="daemon")


def _state_dir_option(state_dir: Path | None) -> Path:
    """Resolve state dir: CLI flag > env > cwd/.agentsos/state."""
    if state_dir is not None:
        return state_dir
    env = os.environ.get("AGENTSOS_STATE_DIR")
    if env:
        return Path(env)
    return Path.cwd() / ".agentsos" / "state"


def _resolve_control_path(state_dir: Path) -> Path:
    return state_dir / "control.json"


def _read_control(state_dir: Path) -> dict:
    p = _resolve_control_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_control(state_dir: Path, payload: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = _resolve_control_path(state_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


@daemon_app.command("status")
def daemon_status(
    state_dir: Path | None = typer.Option(None, "--state-dir", help="Daemon state dir"),
) -> None:
    """Show current daemon state from the snapshot journal."""
    from ...store import Store  # noqa: F401  (sanity import)
    sd = _state_dir_option(state_dir)
    jpath = sd / "journal.jsonl"
    if not jpath.exists():
        console.print(f"[yellow]No daemon journal at {jpath}[/yellow]")
        raise typer.Exit(code=0)
    # Find latest daemon.start/.pause/.resume/.shutdown
    text = jpath.read_text(encoding="utf-8")
    last: dict[str, str] = {}
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("kind", "")
        if kind in ("daemon.start", "daemon.pause", "daemon.resume", "daemon.shutdown"):
            last[kind] = obj.get("ts", "?") + "  " + json.dumps({k: v for k, v in obj.items() if k not in ("ts", "kind", "seq")}, default=str)
    if not last:
        console.print("[yellow]No lifecycle events yet.[/yellow]")
        raise typer.Exit(code=0)
    table = Table(title="Daemon lifecycle", show_header=True)
    table.add_column("event")
    table.add_column("at")
    table.add_column("detail")
    for kind, body in last.items():
        ts, _, rest = body.partition("  ")
        table.add_row(kind, ts, rest)
    console.print(table)


@daemon_app.command("pause")
def daemon_pause(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    reason: str = typer.Option("cli", "--reason", "-r"),
) -> None:
    """Signal the daemon to pause (writes control.json)."""
    sd = _state_dir_option(state_dir)
    _write_control(sd, {"cmd": "pause", "reason": reason, "ts": _now_iso()})
    console.print(f"[cyan]pause[/cyan] → {sd/'control.json'}")


@daemon_app.command("resume")
def daemon_resume(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    reason: str = typer.Option("cli", "--reason", "-r"),
) -> None:
    sd = _state_dir_option(state_dir)
    _write_control(sd, {"cmd": "resume", "reason": reason, "ts": _now_iso()})
    console.print(f"[cyan]resume[/cyan] → {sd/'control.json'}")


@daemon_app.command("stop")
def daemon_stop(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    reason: str = typer.Option("cli", "--reason", "-r"),
) -> None:
    sd = _state_dir_option(state_dir)
    _write_control(sd, {"cmd": "shutdown", "reason": reason, "ts": _now_iso()})
    console.print(f"[red]stop[/red] → {sd/'control.json'}")


@daemon_app.command("start")
def daemon_start(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    ceiling: float = typer.Option(50.0, "--ceiling", help="Daily cost ceiling USD"),
    background: bool = typer.Option(False, "--background", "-b", help="Run detached"),
) -> None:
    """Start the daemon in the foreground (Ctrl-C to stop)."""
    import asyncio
    from ...daemon import Daemon, DaemonConfig

    sd = _state_dir_option(state_dir)
    cfg = DaemonConfig(state_dir=sd, daily_ceiling_usd=ceiling)

    if background:
        # Windows-friendly detach: spawn pythonw.exe if available, else python.
        import subprocess
        import sys
        script = sd.parent / ".hermes" / "daemon_runner.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(_DAEMON_RUNNER_PY, encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(script), str(sd), str(ceiling)],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            stdout=open(sd / "daemon.out.log", "ab"),
            stderr=open(sd / "daemon.err.log", "ab"),
        )
        console.print(f"[green]started[/green] (background, state_dir={sd})")
        return

    async def _run() -> None:
        d = Daemon(cfg)
        await d.start()
        # Wire control-file poller (v0.3.8).
        async def _control_loop() -> None:
            while not d._stop.is_set():
                await asyncio.sleep(0.5)
                ctrl = _read_control(sd)
                if not ctrl:
                    continue
                cmd = ctrl.get("cmd")
                reason = str(ctrl.get("reason", "control-file"))
                # Consume control file (one-shot).
                try:
                    (_resolve_control_path(sd)).unlink()
                except OSError:
                    pass
                if cmd == "pause":
                    await d.pause(reason=reason)
                elif cmd == "resume":
                    await d.resume(reason=reason)
                elif cmd == "shutdown":
                    await d.shutdown(reason=reason)
                    return
        await asyncio.gather(d.wait(), _control_loop())

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="microseconds")


# Minimal detached runner. Writes state to sd/journal.jsonl and
# honours control files just like the foreground version.
_DAEMON_RUNNER_PY = """\
import asyncio, json, sys
from pathlib import Path
from agentsos.daemon import Daemon, DaemonConfig
from agentsos.telegram.bridge import attach_bridge

async def control_loop(d, sd):
    while not d._stop.is_set():
        await asyncio.sleep(0.5)
        cp = sd / "control.json"
        if not cp.exists():
            continue
        try:
            payload = json.loads(cp.read_text(encoding="utf-8"))
            cp.unlink()
        except Exception:
            continue
        cmd, reason = payload.get("cmd"), str(payload.get("reason", "control-file"))
        if cmd == "pause":
            await d.pause(reason=reason)
        elif cmd == "resume":
            await d.resume(reason=reason)
        elif cmd == "shutdown":
            await d.shutdown(reason=reason)
            return

async def main():
    sd = Path(sys.argv[1])
    ceiling = float(sys.argv[2])
    cfg = DaemonConfig(state_dir=sd, daily_ceiling_usd=ceiling,
                       extra_tasks=[attach_bridge()])
    d = Daemon(cfg)
    await d.start()
    await asyncio.gather(d.wait(), control_loop(d, sd))

if __name__ == "__main__":
    asyncio.run(main())
"""


if __name__ == "__main__":
    app()
