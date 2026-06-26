"""CLI for the tokenlab toolkit.

Subcommands:
  count     - exact token count for text or a file
  budget    - show/reset session budget
  audit     - summarize token use across a set of files
  demo      - run a self-contained demo showing savings from compress+cache+trim
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .cache import ResponseCache, request_key
from .compress import Compressor
from .count import count, total
from .schema import minimize_tool_list, schema_size
from .trim import truncate_tool_messages

app = typer.Typer(add_completion=False, help="tokenlab — token optimization toolkit")
console = Console()


@app.command("version")
def version() -> None:
    console.print(f"tokenlab {__version__}")


@app.command("count")
def count_cmd(
    text: Annotated[str | None, typer.Option("--text", "-t")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
) -> None:
    """Count tokens in a string or file."""
    if file is not None:
        text = file.read_text(encoding="utf-8", errors="replace")
    if text is None:
        console.print("[red]pass --text or --file[/red]")
        raise typer.Exit(1)
    n = count(text)
    console.print(f"tokens: {n:,}  chars: {len(text):,}")


@app.command("demo")
def demo() -> None:
    """Run a self-contained demo of compress + cache + trim on a fake
    long-running agent transcript. Prints the savings."""
    # Build a 50-turn "agent" transcript.
    system = {"role": "system", "content": "You are a careful code agent." * 30}
    turns = []
    for i in range(50):
        turns.append({"role": "user", "content": f"step {i}: please run task {i}"})
        turns.append(
            {
                "role": "assistant",
                "content": f"Reading file {i}.txt — got {2000 + i * 7} bytes of output:\n"
                + ("x = 1\n" * 200)
                + f"\n# end of file {i}",
            }
        )
        turns.append(
            {
                "role": "tool",
                "content": "def helper(x):\n    return x * 2\n" * 400,
            }
        )
    messages = [system, *turns]

    before = total(messages)
    console.print(f"baseline tokens: {before:,}")

    # 1. Trim tool messages
    trimmed = truncate_tool_messages(messages, max_bytes=2_000, spill_dir=Path("./traces/spill"))
    after_trim = total(trimmed)
    saved_trim = before - after_trim
    console.print(
        f"after trim:       {after_trim:,}  "
        f"[green]-{saved_trim:,}  (-{saved_trim / before * 100:.0f}%)[/green]"
    )

    # 2. Compress middle
    comp = Compressor(keep_last=8, trigger_at=6_000)
    compressed = comp.compress(trimmed)
    after_comp = total(compressed)
    saved_comp = after_trim - after_comp
    console.print(
        f"after compress:   {after_comp:,}  "
        f"[green]-{saved_comp:,}  (-{saved_comp / after_trim * 100:.0f}%)[/green]"
    )

    # 3. Cache the final request
    cache = ResponseCache(root=Path("./traces/cache"))
    k = request_key("claude-sonnet-4", compressed, tools=[])
    cache.put(k, {"content": "synthetic reply", "model": "claude-sonnet-4"})
    hit = cache.get_exact(k)
    console.print(
        f"cache hit:        {'yes' if hit else 'no'}  entries={cache.stats()['exact_entries']}"
    )

    # 4. Tool schema minimization
    big_tools = [
        {
            "name": f"tool_{i}",
            "description": f"Does thing {i}. This is a fairly long description for tool {i}.",
            "input_schema": {
                "type": "object",
                "title": f"Tool{i}Input",
                "properties": {
                    "path": {"type": "string", "default": "/tmp/x", "description": "File path."},
                    "count": {
                        "type": "integer",
                        "examples": [1, 2, 3],
                        "description": "Number of times to run.",
                    },
                    "flag": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to flag.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        }
        for i in range(20)
    ]
    big = schema_size(big_tools)
    small = schema_size(minimize_tool_list(big_tools))
    console.print(
        f"tool schemas:     {big:,} -> {small:,} bytes  "
        f"[green]-{big - small:,}  (-{(1 - small / big) * 100:.0f}%)[/green]"
    )

    total_saved = before - after_comp
    console.print(
        f"\n[bold green]TOTAL reduction:  {total_saved:,} tokens  "
        f"({(1 - after_comp / before) * 100:.0f}%)[/bold green]"
    )


@app.command("audit")
def audit_cmd(files: list[Path]) -> None:
    """Print a token-count table for a list of files."""
    table = Table("file", "chars", "tokens")
    grand = 0
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        n = count(text)
        grand += n
        table.add_row(str(f), f"{len(text):,}", f"{n:,}")
    table.add_row("TOTAL", "", f"{grand:,}")
    console.print(table)


if __name__ == "__main__":
    app()
