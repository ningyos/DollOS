"""Tests for VitalsRecorder — per-turn metabolic vitals JSONL (RL substrate)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dollos.telemetry.vitals import VitalsRecord, VitalsRecorder


@pytest.mark.asyncio
async def test_vitals_recorder_writes_row(tmp_path: Path):
    rec = VitalsRecorder(tmp_path)
    await rec.record(VitalsRecord(
        ts=1_000_000.0, turn_id="t-abc", tokens_total=1100,
        energy_cost=0.55, energy_before=1.0, energy_after=0.45, cost_mode="measured",
    ))
    files = list(tmp_path.glob("vitals-*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text().strip())
    assert row["turn_id"] == "t-abc" and row["energy_cost"] == 0.55
    assert row["cost_mode"] == "measured" and row["tokens_total"] == 1100
    assert row["energy_after"] == 0.45
    # v1b ambient fields default None until Task 5 fills them.
    assert row["gpu_hottest_c"] is None
    assert row["gpu_power_w"] is None
    assert row["battery_pct"] is None


@pytest.mark.asyncio
async def test_vitals_recorder_never_raises_on_bad_dir(tmp_path: Path):
    # 指向一個是檔案的路徑，mkdir 會失敗——record 必須吞掉、不 raise
    bad = tmp_path / "afile"
    bad.write_text("x")
    rec = VitalsRecorder(bad)
    await rec.record(VitalsRecord(
        ts=1.0, turn_id=None, tokens_total=None,
        energy_cost=0.0, energy_before=1.0, energy_after=1.0, cost_mode="flat_legacy",
    ))   # 不得 raise
