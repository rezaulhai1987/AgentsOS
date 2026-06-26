"""Built-in tools. Each tool is a registered callable with a JSON schema.

The tool registry lives in `agentsos.registry.ToolRegistry`.
"""

from __future__ import annotations

from pathlib import Path


def echo(text: str) -> str:
    """Return the input text unchanged. Useful for tests and as a no-op."""
    return text


def read_file(path: str, max_bytes: int = 1_000_000) -> str:
    """Read a UTF-8 text file. Refuses paths above 1MB to avoid OOM on agents."""
    p = Path(path)
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"{path}: {len(data)} bytes exceeds cap {max_bytes}")
    return data.decode("utf-8", errors="replace")


def write_file(path: str, content: str) -> str:
    """Write text to a file, creating parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def list_dir(path: str = ".") -> list[str]:
    """List immediate children of a directory."""
    return sorted(p.name for p in Path(path).iterdir())


BUILTINS = {
    "echo": echo,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
}
