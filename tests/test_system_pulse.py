"""SystemPulse — bucketing, change-detection, rendering, lifecycle."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from dollos.perception.system_pulse import (
    PulseSample,
    SystemPulse,
    bucket_battery,
    bucket_gpu_temp,
    bucket_idle,
    bucket_load,
    bucket_mem,
    read_nvidia_smi,
    render_block,
)


# ---------------------------------------------------------------- buckets

def test_bucket_load():
    assert bucket_load(0.1, 8) == "light"
    assert bucket_load(3.0, 8) == "light"   # 0.375
    assert bucket_load(4.0, 8) == "steady"  # 0.5
    assert bucket_load(7.9, 8) == "steady"
    assert bucket_load(8.0, 8) == "heavy"   # 1.0
    assert bucket_load(20.0, 8) == "heavy"
    # zero cpu is treated as 1
    assert bucket_load(0.1, 0) == "light"


def test_bucket_mem():
    assert bucket_mem(10.0) == "comfortable"
    assert bucket_mem(59.9) == "comfortable"
    assert bucket_mem(60.0) == "tight"
    assert bucket_mem(84.9) == "tight"
    assert bucket_mem(85.0) == "pressured"
    assert bucket_mem(99.9) == "pressured"


def test_bucket_gpu_temp():
    assert bucket_gpu_temp(30) == "cool"
    assert bucket_gpu_temp(54.9) == "cool"
    assert bucket_gpu_temp(55) == "warm"
    assert bucket_gpu_temp(74.9) == "warm"
    assert bucket_gpu_temp(75) == "hot"
    assert bucket_gpu_temp(90) == "hot"


def test_bucket_battery():
    assert bucket_battery(100) == "full"
    assert bucket_battery(95) == "full"
    assert bucket_battery(94.9) == "healthy"
    assert bucket_battery(30) == "healthy"
    assert bucket_battery(29.9) == "low"
    assert bucket_battery(15) == "low"
    assert bucket_battery(14.9) == "critical"
    assert bucket_battery(0) == "critical"


def test_bucket_idle():
    assert bucket_idle(0) == "present"
    assert bucket_idle(59.9) == "present"
    assert bucket_idle(60) == "away"
    assert bucket_idle(599) == "away"
    assert bucket_idle(600) == "long-away"
    assert bucket_idle(10000) == "long-away"


# ------------------------------------------------------------- signature

def test_signature_same_bucket_same_signature():
    a = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 0, 0),
        load1=0.2, ncpu=8, mem_used_pct=40.0,
        gpus=[(20.0, 50.0)],
        battery_pct=80.0, battery_status="Charging",
        active_window="vscode", idle_s=10.0, network_open=True,
    )
    b = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 1, 0),
        load1=0.3, ncpu=8, mem_used_pct=42.0,    # same buckets
        gpus=[(22.0, 51.0)],
        battery_pct=82.0, battery_status="Charging",
        active_window="vscode", idle_s=15.0, network_open=True,
    )
    assert a.signature() == b.signature()


def test_signature_bucket_change_differs():
    a = PulseSample(
        taken_at=datetime.now(),
        load1=0.2, ncpu=8, mem_used_pct=40.0, gpus=[],
        idle_s=10.0, network_open=True,
    )
    b = PulseSample(
        taken_at=datetime.now(),
        load1=0.2, ncpu=8, mem_used_pct=90.0, gpus=[],   # pressured
        idle_s=10.0, network_open=True,
    )
    assert a.signature() != b.signature()


def test_signature_active_window_change():
    a = PulseSample(taken_at=datetime.now(), active_window="vscode")
    b = PulseSample(taken_at=datetime.now(), active_window="firefox")
    assert a.signature() != b.signature()


# --------------------------------------------------------------- render

def test_render_block_complete():
    s = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 34, 12),
        load1=1.2, ncpu=8, mem_used_pct=38.0,
        gpus=[(45.0, 42.0), (12.0, 51.0)],
        battery_pct=87.0, battery_status="Charging",
        active_window="VSCode — tts_qwen3.py",
        idle_s=12.0, network_open=True,
    )
    text = render_block(s, include_active_window=True)
    assert text.startswith("[Self pulse 21:34:12]")
    assert "thought load: light" in text
    assert "memory: comfortable (38%)" in text
    assert "vital heat: cool" in text
    assert "42°C / 51°C" in text
    assert "charge: healthy" in text
    assert "plugged in" in text
    assert "attention: VSCode — tts_qwen3.py" in text
    assert "breath since last input: present (12s ago)" in text
    assert "outside link: open" in text


def test_render_block_gpu_power_presence_gated():
    s = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 34, 12),
        gpus=[(45.0, 42.0), (12.0, 51.0)],
        gpu_power=[(120.5, 165.0), (90.0, 165.0)],
    )
    text = render_block(s, include_active_window=True)
    assert "vital heat: cool" in text
    assert "· 120w" in text  # max draw across GPUs, rounded


def test_render_block_omits_power_suffix_when_gpu_power_empty():
    s = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 34, 12),
        gpus=[(45.0, 42.0)],
    )
    text = render_block(s, include_active_window=True)
    assert "vital heat: cool" in text
    assert "w" not in text.split("vital heat")[1].split("\n")[0]


def test_render_block_omits_missing_fields():
    s = PulseSample(taken_at=datetime(2026, 5, 18, 21, 0, 0), load1=0.5, ncpu=4)
    text = render_block(s, include_active_window=True)
    assert "thought load" in text
    assert "memory" not in text
    assert "vital heat" not in text
    assert "charge" not in text
    assert "attention" not in text
    assert "outside link" not in text


def test_render_block_active_window_opt_out():
    s = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 0, 0),
        active_window="some secret window",
    )
    text = render_block(s, include_active_window=False)
    assert "attention" not in text
    assert "some secret window" not in text


def test_render_block_battery_discharging():
    s = PulseSample(
        taken_at=datetime(2026, 5, 18, 21, 0, 0),
        battery_pct=20.0, battery_status="Discharging",
    )
    text = render_block(s, include_active_window=False)
    assert "charge: low" in text
    assert "on battery" in text


# -------------------------------------------------- snapshot / change-detect

@pytest.mark.asyncio
async def test_snapshot_first_call_returns_text():
    sp = SystemPulse(poll_interval_s=999.0, include_active_window=False)
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        load1=0.5, ncpu=4, mem_used_pct=40.0,
    )
    out = sp.snapshot()
    assert out is not None
    assert "[Self pulse" in out


@pytest.mark.asyncio
async def test_snapshot_unchanged_returns_none():
    sp = SystemPulse(poll_interval_s=999.0, include_active_window=False)
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        load1=0.5, ncpu=4, mem_used_pct=40.0,
    )
    first = sp.snapshot()
    assert first is not None
    # Same bucket → None
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        load1=0.6, ncpu=4, mem_used_pct=42.0,
    )
    assert sp.snapshot() is None


@pytest.mark.asyncio
async def test_snapshot_bucket_change_returns_text_again():
    sp = SystemPulse(poll_interval_s=999.0, include_active_window=False)
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        load1=0.5, ncpu=4, mem_used_pct=40.0,
    )
    sp.snapshot()  # emit
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        load1=0.5, ncpu=4, mem_used_pct=90.0,   # pressured
    )
    out = sp.snapshot()
    assert out is not None
    assert "memory: pressured" in out


@pytest.mark.asyncio
async def test_snapshot_no_sample_returns_none():
    sp = SystemPulse(poll_interval_s=999.0)
    assert sp.snapshot() is None


@pytest.mark.asyncio
async def test_snapshot_disabled_returns_none():
    sp = SystemPulse(poll_interval_s=999.0, enabled=False)
    sp._last_sample = PulseSample(taken_at=datetime.now(), load1=0.5, ncpu=4)
    assert sp.snapshot() is None


# ---------------------------------------------------------- lifecycle

@pytest.mark.asyncio
async def test_lifecycle_disabled_no_task():
    sp = SystemPulse(poll_interval_s=0.05, enabled=False)
    sp.start()
    assert sp._task is None
    await sp.stop()


@pytest.mark.asyncio
async def test_poll_now_populates_sample():
    sp = SystemPulse(poll_interval_s=999.0, include_active_window=False)
    # Patch nvidia/xprintidle/network to return nothing fast
    with patch(
        "dollos.perception.system_pulse.read_nvidia_smi",
        return_value=([], []),
    ), patch(
        "dollos.perception.system_pulse.read_idle_seconds",
        return_value=None,
    ), patch(
        "dollos.perception.system_pulse.check_network",
        return_value=False,
    ):
        await sp.poll_now()
    assert sp._last_sample is not None
    # /proc/loadavg should be readable on Linux
    assert sp._last_sample.load1 is not None
    assert sp._last_sample.mem_used_pct is not None


def _const_async(value):
    """Helper: an async function that ignores its args and always returns `value`.

    Used to monkeypatch `_run_cmd` (which takes argv/timeout_s args) so
    `read_nvidia_smi` sees a fixed fake CSV string on every call.
    """
    async def _fake(*args, **kwargs):
        return value
    return _fake


# ---------------------------------------------------------- nvidia power

@pytest.mark.asyncio
async def test_nvidia_parses_power_draw_parallel(monkeypatch):
    # fake nvidia-smi CSV: memory.used, memory.total, temperature.gpu, power.draw, power.limit
    fake = "1024, 8192, 70, 120.5, 165.0\n"
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(fake))
    gpus, gpu_power = await read_nvidia_smi()
    assert gpus == [(1024 / 8192 * 100.0, 70.0)]
    assert gpu_power == [(120.5, 165.0)]


@pytest.mark.asyncio
async def test_nvidia_power_absent_columns_still_parses_mem_temp(monkeypatch):
    fake = "1024, 8192, 70\n"  # old 3-col output (driver without power telemetry)
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(fake))
    gpus, gpu_power = await read_nvidia_smi()
    assert gpus == [(1024 / 8192 * 100.0, 70.0)]
    assert gpu_power == []  # defensive: power omitted, mem/temp intact


@pytest.mark.asyncio
async def test_nvidia_power_partial_column_failure_still_parses_mem_temp(monkeypatch):
    """A malformed power column (e.g. '[N/A]') must not drop mem/temp for that line."""
    fake = "1024, 8192, 70, [N/A], [N/A]\n"
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(fake))
    gpus, gpu_power = await read_nvidia_smi()
    assert gpus == [(1024 / 8192 * 100.0, 70.0)]
    assert gpu_power == []


@pytest.mark.asyncio
async def test_nvidia_no_smi_returns_empty_pair(monkeypatch):
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(None))
    gpus, gpu_power = await read_nvidia_smi()
    assert gpus == []
    assert gpu_power == []


def test_latest_idle_s_none_when_no_sample():
    from dollos.perception.system_pulse import SystemPulse
    sp = SystemPulse(poll_interval_s=60.0, enabled=False)
    assert sp.latest_idle_s() is None


def test_latest_idle_s_none_when_disabled_even_with_sample():
    """enabled=False must return None even if a sample with idle_s is present."""
    sp = SystemPulse(poll_interval_s=60.0, enabled=False)
    # Inject a sample directly — enabled=False means the poller never ran,
    # but the guard must still fire regardless of how _last_sample got set.
    sp._last_sample = PulseSample(
        taken_at=datetime.now(),
        idle_s=30.0,
    )
    assert sp.latest_idle_s() is None


def test_latest_idle_s_none_when_stale(monkeypatch):
    """Sample whose taken_at is older than 2 * poll_interval_s → None."""
    sp = SystemPulse(poll_interval_s=60.0, enabled=True)
    # Set taken_at well in the past (beyond 2 * 60 = 120 s).
    stale_time = datetime(2020, 1, 1, 0, 0, 0)
    sp._last_sample = PulseSample(
        taken_at=stale_time,
        idle_s=45.0,
    )
    assert sp.latest_idle_s() is None


def test_latest_sample_none_when_no_sample():
    sp = SystemPulse(poll_interval_s=60.0, enabled=True)
    assert sp.latest_sample() is None


def test_latest_sample_none_when_disabled():
    sp = SystemPulse(poll_interval_s=60.0, enabled=False)
    sp._last_sample = PulseSample(taken_at=datetime.now(), load1=1.0, ncpu=8)
    assert sp.latest_sample() is None


def test_latest_sample_returns_fresh():
    sp = SystemPulse(poll_interval_s=60.0, enabled=True)
    s = PulseSample(taken_at=datetime.now(), load1=1.0, ncpu=8)
    sp._last_sample = s
    assert sp.latest_sample() is s


def test_latest_sample_none_when_stale():
    sp = SystemPulse(poll_interval_s=60.0, enabled=True)
    sp._last_sample = PulseSample(
        taken_at=datetime.now() - timedelta(seconds=200), load1=1.0, ncpu=8
    )
    assert sp.latest_sample() is None
