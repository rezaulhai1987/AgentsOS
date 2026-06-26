"""Operator CLI — the `agents` command.

Currently exposes:
- `agents list-templates`
- `agents run --template <name> --goal "<text>"`
- `agents validate <path-to-manifest>`
"""

from __future__ import annotations

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


if __name__ == "__main__":
    app()
