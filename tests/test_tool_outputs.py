from pathlib import Path

import pytest

from dollos.tool_outputs import ToolOutputStore


def test_write_then_read_full(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\nworld\nthis is line three\n")
    assert output_id.startswith("out-")
    slice_ = store.read(output_id, offset=0, limit=10)
    assert slice_.lines == ["hello", "world", "this is line three"]
    assert slice_.total_lines == 3
    assert slice_.start_offset == 0
    assert slice_.end_offset == 3


def test_line_count(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("a\nb\nc\nd\n")
    assert store.line_count(output_id) == 4


def test_paging(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"line {i}" for i in range(100)))
    s = store.read(output_id, offset=10, limit=5)
    assert s.lines == ["line 10", "line 11", "line 12", "line 13", "line 14"]
    assert s.start_offset == 10
    assert s.end_offset == 15
    assert s.total_lines == 100


def test_negative_offset_seeks_from_end(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"line {i}" for i in range(20)))
    s = store.read(output_id, offset=-3, limit=10)
    assert s.lines == ["line 17", "line 18", "line 19"]


def test_grep_matches(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("error: foo\nok\nerror: bar\nok\nerror: baz\n")
    matches = store.grep(output_id, pattern=r"^error:", max_matches=2)
    assert [m.line_index for m in matches] == [0, 2]
    assert matches[0].line == "error: foo"


def test_invalid_id_raises(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../etc/passwd", offset=0, limit=10)
    with pytest.raises(FileNotFoundError):
        store.read("out-deadbeef", offset=0, limit=10)


def test_cleanup_idempotent(tmp_path: Path) -> None:
    store_root = tmp_path / "ephemeral"
    store = ToolOutputStore(store_root)
    store.write("x")
    assert store_root.exists()
    store.cleanup()
    assert not store_root.exists()
    store.cleanup()  # second call is a no-op
