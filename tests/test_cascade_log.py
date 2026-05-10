"""Tests for cascade_log: think parsing + JSONL output."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from dollos.cascade_log import CascadeLogger, _parse_think
from dollos.logging_config import configure_cascade_logging


@dataclass
class _FakeToolResult:
    tool_name: str
    success: bool
    detail: str


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


def test_parse_think_extracts_all_5_fields():
    text = (
        "<think>\n"
        "SEEN: user said hi\n"
        "INTENT: respond warmly\n"
        "REVIEW: no prior context\n"
        "MOOD: 平靜\n"
        "TOOL: Say\n"
        "</think>"
    )
    out = _parse_think(text)
    assert out == {
        "seen": "user said hi",
        "intent": "respond warmly",
        "review": "no prior context",
        "mood": "平靜",
        "tool": "Say",
    }


def test_parse_think_handles_missing_fields():
    text = "SEEN: just this\nMOOD: 開心"
    out = _parse_think(text)
    assert out == {"seen": "just this", "mood": "開心"}


def test_log_iter_writes_jsonl_line(tmp_path: Path):
    logger = _setup(tmp_path)
    logger.log_iter(
        turn_id="t1",
        iter=1,
        assistant_text="SEEN: hi\nINTENT: greet\nMOOD: ok\nTOOL: Say",
        tool_calls=[{"name": "Say", "arguments": {"text": "hi"}}],
        results=[_FakeToolResult("Say", True, "")],
        duration_ms=42,
    )
    rows = _read_log(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"] == "t1"
    assert row["iter"] == 1
    assert row["seen"] == "hi"
    assert row["intent"] == "greet"
    assert row["mood"] == "ok"
    assert row["tool"] == "Say"
    assert row["tool_calls"] == [{"name": "Say", "args": {"text": "hi"}}]
    assert row["results"] == [{"tool_name": "Say", "success": True, "detail": ""}]
    assert row["duration_ms"] == 42
    assert row["event"] == "cascade_iter"


def test_log_iter_truncates_long_tool_detail(tmp_path: Path):
    logger = _setup(tmp_path)
    long_detail = "x" * 5000
    logger.log_iter(
        turn_id="t2",
        iter=1,
        assistant_text="",
        tool_calls=[],
        results=[_FakeToolResult("Shell", True, long_detail)],
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
        assistant_text=None,  # type: ignore[arg-type]
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
