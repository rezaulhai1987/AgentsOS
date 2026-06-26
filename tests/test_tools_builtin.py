"""Tests for built-in tools."""

from __future__ import annotations

from agentsos.tools_builtin import echo, list_dir, read_file, write_file


def test_echo() -> None:
    assert echo("hi") == "hi"


def test_write_and_read(tmp_path) -> None:
    p = tmp_path / "x.txt"
    write_file(str(p), "hello")
    assert read_file(str(p)) == "hello"


def test_read_caps_oversize(tmp_path) -> None:
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * 2000)
    import pytest

    with pytest.raises(ValueError):
        read_file(str(p), max_bytes=100)


def test_list_dir(tmp_path) -> None:
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    assert set(list_dir(str(tmp_path))) == {"a", "b"}
