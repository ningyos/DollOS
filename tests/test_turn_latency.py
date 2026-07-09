"""Tests for TurnLatencyRecorder — turn-level think/speak/first-speak JSONL."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dollos.telemetry.turn_latency import TurnLatencyRecord, TurnLatencyRecorder


@pytest.mark.asyncio
async def test_record_appends_jsonl(tmp_path: Path):
    rec = TurnLatencyRecorder(tmp_path)
    r = TurnLatencyRecord(ts=1780000000.0, first_speak_ms=1840.0,
                          think_chars=90, speak_chars=40, total_ms=2100.0,
                          ttft_ms=1600.0, mode="deliberate", n_passes=1,
                          had_tool_call=False,
                          perception_kinds=["UserSpoke"], turn_id="abc123")
    await rec.record(r)
    files = list(tmp_path.glob("turn_latency-*.jsonl"))
    assert len(files) == 1
    d = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert d["first_speak_ms"] == 1840.0
    assert d["think_chars"] == 90 and d["speak_chars"] == 40
    assert d["mode"] == "deliberate"
    assert d["perception_kinds"] == ["UserSpoke"]
    assert d["turn_id"] == "abc123"


@pytest.mark.asyncio
async def test_record_never_raises_on_bad_dir(tmp_path: Path):
    # 指向一個是檔案的路徑，mkdir 會失敗——record 必須吞掉、不 raise
    bad = tmp_path / "afile"
    bad.write_text("x")
    rec = TurnLatencyRecorder(bad)
    r = TurnLatencyRecord(ts=1.0, first_speak_ms=None, think_chars=0,
                          speak_chars=0, total_ms=1.0, ttft_ms=None,
                          mode="deliberate", n_passes=1, had_tool_call=False,
                          perception_kinds=[], turn_id=None)
    await rec.record(r)   # 不得 raise
