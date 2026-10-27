"""Tests for cascade_log: raw reasoning + speech + JSONL output."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dollos.harness.executor import ToolResult
from dollos.cascade_log import CascadeLogger
from dollos.logging_config import configure_cascade_logging


def _setup(tmp_path: Path) -> CascadeLogger:
    configure_cascade_logging(tmp_path)
    return CascadeLogger(tmp_path)


def _read_log(tmp_path: Path) -> list[dict]:
    # configure_cascade_logging picks {today}.jsonl; find any jsonl present.
    for f in tmp_path.iterdir():
        if f.suffix == ".jsonl":
            # Flush any open handlers so file content is visible.
            for h in logging.getLogger("cascade").handlers:
                h.flush()
            return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return []


def test_log_iter_writes_jsonl_line(tmp_path: Path):
    logger = _setup(tmp_path)
    logger.log_iter(
        turn_id="t1",
        iter=1,
        reasoning="The user said hi.\nI'll greet back and note it.",
        speech="Hi!",
        tool_calls=[{"id": "call_0", "name": "NoteMemory", "arguments": '{"text": "hi"}'}],
        results=[
            ToolResult("NoteMemory", True, "noted"),
            ToolResult("Shell", False, "Shell is not available", unbound=True),
        ],
        duration_ms=42,
    )
    rows = _read_log(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"] == "t1"
    assert row["iter"] == 1
    assert row["reasoning"] == "The user said hi.\nI'll greet back and note it."
    assert row["speech"] == "Hi!"
    assert row["tool_calls"] == [
        {"id": "call_0", "name": "NoteMemory", "args": '{"text": "hi"}'}
    ]
    assert row["results"] == [
        {"tool_name": "NoteMemory", "success": True, "status": "ok", "detail": "noted"},
        {"tool_name": "Shell", "success": False, "status": "rejected_unbound",
         "detail": "Shell is not available"},
    ]
    assert row["duration_ms"] == 42
    assert row["event"] == "cascade_iter"
    # No parsed think fields any more.
    assert not {"seen", "intent", "review", "mood", "tool"} & set(row)


def test_log_iter_caps_reasoning_at_4000_chars(tmp_path: Path):
    logger = _setup(tmp_path)
    logger.log_iter(turn_id="t4", iter=0, reasoning="r" * 9000, speech="")
    rows = _read_log(tmp_path)
    assert rows[0]["reasoning"] == "r" * 4000


def test_log_iter_truncates_long_tool_detail(tmp_path: Path):
    logger = _setup(tmp_path)
    long_detail = "x" * 5000
    logger.log_iter(
        turn_id="t2",
        iter=1,
        reasoning="",
        speech="",
        tool_calls=[],
        results=[ToolResult("Shell", True, long_detail)],
        duration_ms=1,
    )
    rows = _read_log(tmp_path)
    assert len(rows) == 1
    assert len(rows[0]["results"][0]["detail"]) == 500


def test_log_iter_swallows_exceptions(tmp_path: Path):
    logger = _setup(tmp_path)
    # Pass non-iterable tool_calls type; log_iter must not raise.
    logger.log_iter(
        turn_id="t3",
        iter=1,
        reasoning=None,  # type: ignore[arg-type]
        speech="",
        tool_calls=12345,  # type: ignore[arg-type]
        results=None,
        duration_ms=0,
    )
    # No exception => pass.


def test_start_turn_returns_unique_id(tmp_path: Path):
    logger = _setup(tmp_path)
    a = logger.start_turn()
    b = logger.start_turn()
    assert a != b
    assert isinstance(a, str) and isinstance(b, str)
