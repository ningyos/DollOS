"""Cognition vitals — telemetry, bucketing, change-on-significance, render."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from dollos.perception.cognition import (
    CognitionSnapshot,
    CognitionWorker,
    bucket_mind_room,
    bucket_quota,
    bucket_strain,
    bucket_thought_weight,
    render_block,
)
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder


# -------------------------------------------------------- bucketing

def test_bucket_mind_room():
    assert bucket_mind_room(0) == "plenty"
    assert bucket_mind_room(49.9) == "plenty"
    assert bucket_mind_room(50) == "room"
    assert bucket_mind_room(74.9) == "room"
    assert bucket_mind_room(75) == "getting full"
    assert bucket_mind_room(89.9) == "getting full"
    assert bucket_mind_room(90) == "near limit"
    assert bucket_mind_room(99) == "near limit"


def test_bucket_thought_weight():
    # avg None → steady
    assert bucket_thought_weight(2000, None) == "steady"
    assert bucket_thought_weight(2000, 0) == "steady"
    # ratios
    assert bucket_thought_weight(500, 1000) == "light"     # 0.5
    assert bucket_thought_weight(900, 1000) == "steady"    # 0.9
    assert bucket_thought_weight(1499, 1000) == "steady"   # 1.499
    assert bucket_thought_weight(1500, 1000) == "heavy"    # 1.5
    assert bucket_thought_weight(1999, 1000) == "heavy"
    assert bucket_thought_weight(2000, 1000) == "very heavy"


def test_bucket_quota():
    assert bucket_quota(0) == "fresh"
    assert bucket_quota(24.9) == "fresh"
    assert bucket_quota(25) == "warmed up"
    assert bucket_quota(49.9) == "warmed up"
    assert bucket_quota(50) == "used"
    assert bucket_quota(74.9) == "used"
    assert bucket_quota(75) == "strained"
    assert bucket_quota(89.9) == "strained"
    assert bucket_quota(90) == "almost spent"


def test_bucket_strain():
    assert bucket_strain(0) == "calm"
    assert bucket_strain(1) == "minor"
    assert bucket_strain(2) == "minor"
    assert bucket_strain(3) == "notable"
    assert bucket_strain(10) == "notable"


# -------------------------------------------------------- recorder

@pytest.mark.asyncio
async def test_recorder_append_and_read_today(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "telemetry")
    r1 = LLMCallRecord(
        ts=time.time(),
        model="Qwen3.6",
        prompt_tokens=1000,
        completion_tokens=50,
        latency_ttft_ms=120,
        latency_total_ms=1500,
        context_pct=0.76,
        call_purpose="cascade",
    )
    await rec.record(r1)
    r2 = LLMCallRecord(
        ts=time.time(),
        prompt_tokens=2000,
        completion_tokens=100,
        latency_total_ms=2000,
    )
    await rec.record(r2)

    out = rec.read_today()
    assert len(out) == 2
    assert out[0].model == "Qwen3.6"
    assert out[0].prompt_tokens == 1000
    assert out[1].prompt_tokens == 2000

    # File on disk should be append-only JSONL
    files = list((tmp_path / "telemetry").glob("llm_calls-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["model"] == "Qwen3.6"


@pytest.mark.asyncio
async def test_recorder_handles_missing_fields(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "telemetry")
    r = LLMCallRecord(ts=time.time())  # everything else None
    await rec.record(r)
    out = rec.read_today()
    assert len(out) == 1
    assert out[0].prompt_tokens is None
    assert out[0].completion_tokens is None
    assert out[0].error is None


@pytest.mark.asyncio
async def test_recorder_creates_dir(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "telemetry"
    rec = TelemetryRecorder(nested)
    await rec.record(LLMCallRecord(ts=time.time(), prompt_tokens=10))
    assert nested.exists()
    assert list(nested.glob("llm_calls-*.jsonl"))


# ----------------------------------- CognitionWorker / snapshot

@pytest.mark.asyncio
async def test_snapshot_no_records_returns_none(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    w = CognitionWorker(recorder=rec)
    assert w.snapshot() is None


@pytest.mark.asyncio
async def test_snapshot_first_emit_returns_block(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=10_000,
        completion_tokens=300,
        latency_total_ms=2000,
        context_pct=10_000 / 131_072 * 100,
    ))
    w = CognitionWorker(recorder=rec, daily_token_quota=500_000)
    out = w.snapshot()
    assert out is not None
    assert out.startswith("[Cognition")
    assert "mind room: plenty" in out
    assert "strain: calm" in out
    assert "quota:" in out


@pytest.mark.asyncio
async def test_snapshot_unchanged_returns_none(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=10_000,
        completion_tokens=300,
        latency_total_ms=2000,
        context_pct=10_000 / 131_072 * 100,
    ))
    w = CognitionWorker(recorder=rec, daily_token_quota=500_000)
    first = w.snapshot()
    assert first is not None
    # second call without new data → no bucket change → None
    assert w.snapshot() is None


@pytest.mark.asyncio
async def test_snapshot_bucket_transition_emits_again(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    # initial: plenty mind-room
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=10_000,
        completion_tokens=100,
        latency_total_ms=2000,
        context_pct=10_000 / 131_072 * 100,
    ))
    w = CognitionWorker(recorder=rec, daily_token_quota=500_000)
    w.snapshot()  # emit
    # next call: near-limit context
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=120_000,
        completion_tokens=100,
        latency_total_ms=2200,
        context_pct=120_000 / 131_072 * 100,
    ))
    out = w.snapshot()
    assert out is not None
    assert "mind room: near limit" in out


@pytest.mark.asyncio
async def test_snapshot_new_error_forces_emit(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=10_000,
        completion_tokens=100,
        latency_total_ms=2000,
        context_pct=10_000 / 131_072 * 100,
    ))
    w = CognitionWorker(recorder=rec, daily_token_quota=500_000)
    w.snapshot()  # baseline
    # buckets identical but a new error appears
    await rec.record(LLMCallRecord(
        ts=time.time(),
        prompt_tokens=10_500,
        completion_tokens=100,
        latency_total_ms=2100,
        context_pct=10_500 / 131_072 * 100,
        error="timeout",
    ))
    out = w.snapshot()
    assert out is not None
    assert "strain: minor" in out


@pytest.mark.asyncio
async def test_snapshot_disabled_returns_none(tmp_path: Path):
    rec = TelemetryRecorder(tmp_path / "tel")
    await rec.record(LLMCallRecord(ts=time.time(), prompt_tokens=100))
    w = CognitionWorker(recorder=rec, enabled=False)
    assert w.snapshot() is None


# ----------------------------------- render with missing fields

def test_render_omits_missing_fields():
    snap = CognitionSnapshot(
        taken_at=datetime(2026, 5, 18, 21, 34, 12),
        # all token-related fields None
        context_pct=None,
        last_prompt_tokens=None,
        max_context_tokens=131_072,
        last_latency_ms=None,
        trailing_avg_ms=None,
        tokens_used_today=None,
        daily_token_quota=500_000,
        recent_error_count=0,
    )
    text = render_block(snap)
    assert text.startswith("[Cognition 21:34:12]")
    assert "mind room" not in text       # missing context_pct
    assert "thought weight" not in text  # missing latency
    assert "quota" not in text           # tokens_used_today None → omit
    assert "strain: calm" in text


def test_render_thought_weight_no_trailing_avg():
    snap = CognitionSnapshot(
        taken_at=datetime(2026, 5, 18, 21, 0, 0),
        last_latency_ms=2400,
        trailing_avg_ms=None,
        recent_error_count=0,
    )
    text = render_block(snap)
    assert "thought weight: steady (last 2.4s)" in text
    assert "normally" not in text


def test_render_with_quota_disabled():
    snap = CognitionSnapshot(
        taken_at=datetime(2026, 5, 18, 21, 0, 0),
        last_latency_ms=2000,
        tokens_used_today=50_000,
        daily_token_quota=None,
        recent_error_count=0,
    )
    text = render_block(snap)
    assert "quota" not in text


def test_render_full_block():
    snap = CognitionSnapshot(
        taken_at=datetime(2026, 5, 18, 21, 34, 12),
        context_pct=110_000 / 131_072 * 100,
        last_prompt_tokens=110_000,
        max_context_tokens=131_072,
        last_latency_ms=8100,
        trailing_avg_ms=3000,
        tokens_used_today=480_000,
        daily_token_quota=500_000,
        recent_error_count=2,
        last_error="timeout",
    )
    text = render_block(snap)
    assert "[Cognition 21:34:12]" in text
    assert "mind room: getting full" in text
    assert "thought weight: very heavy" in text
    assert "8.1s" in text
    assert "normally 3.0s" in text
    assert "quota: almost spent" in text
    assert "strain: minor (2 errors" in text
