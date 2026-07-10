from datetime import datetime

import pytest

from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.pulse_observer import Alert, AlertState, PulseObserver, evaluate_alerts
from dollos.perception.system_pulse import PulseSample

THROTTLE = 900.0
STUCK = 5400.0


def _sample(**kw) -> PulseSample:
    return PulseSample(taken_at=datetime.now(), **kw)


# --- battery_critical edge + re-arm ---

def test_battery_critical_fires_once_on_edge():
    st = AlertState.initial()
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    alerts, st2 = evaluate_alerts(st, s, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["battery_critical"]
    assert st2.battery_armed is False
    # still critical next poll → no re-fire
    alerts2, st3 = evaluate_alerts(st2, s, 10_100.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts2 == []


def test_battery_critical_not_fired_when_charging():
    st = AlertState.initial()
    s = _sample(battery_pct=12.0, battery_status="Charging")
    alerts, st2 = evaluate_alerts(st, s, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts == []
    assert st2.battery_armed is True


def test_battery_rearms_after_recovery():
    st = AlertState.initial()
    crit = _sample(battery_pct=12.0, battery_status="Discharging")
    _, st = evaluate_alerts(st, crit, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.battery_armed is False
    # recovers (charging) → re-arm
    ok = _sample(battery_pct=40.0, battery_status="Charging")
    _, st = evaluate_alerts(st, ok, 20_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.battery_armed is True
    # critical again → fires again
    alerts, st = evaluate_alerts(st, crit, 30_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["battery_critical"]


# --- gpu_hot edge + re-arm ---

def test_gpu_hot_fires_on_edge_and_rearms():
    st = AlertState.initial()
    hot = _sample(gpus=[(50.0, 80.0)])
    alerts, st = evaluate_alerts(st, hot, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["gpu_hot"]
    # persists → no re-fire
    alerts, st = evaluate_alerts(st, hot, 10_100.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts == []
    # cools → re-arm
    cool = _sample(gpus=[(50.0, 50.0)])
    _, st = evaluate_alerts(st, cool, 10_200.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.gpu_armed is True


# --- window_stuck accumulator ---

def test_window_stuck_fires_after_threshold_while_present():
    st = AlertState.initial()
    s0 = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, s0, 0.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.stuck_window == "editor" and st.stuck_since == 0.0
    # 90 min later, same window, present → fires
    s1 = _sample(active_window="editor", idle_s=5.0)
    alerts, st = evaluate_alerts(st, s1, STUCK, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["window_stuck"]
    assert st.stuck_armed is False


def test_window_change_resets_streak():
    st = AlertState.initial()
    s0 = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, s0, 0.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    s1 = _sample(active_window="browser", idle_s=5.0)
    alerts, st = evaluate_alerts(st, s1, STUCK, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts == []
    assert st.stuck_window == "browser" and st.stuck_since == STUCK


def test_window_stuck_streak_broken_by_stepping_away():
    st = AlertState.initial()
    s0 = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, s0, 0.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    # away (idle high) on same window → streak resets
    away = _sample(active_window="editor", idle_s=300.0)
    _, st = evaluate_alerts(st, away, 1000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.stuck_since is None
    # back present, only 1 min elapsed since return → no fire
    back = _sample(active_window="editor", idle_s=5.0)
    alerts, st = evaluate_alerts(st, back, 1060.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts == []
    assert st.stuck_since == 1060.0


def test_window_stuck_rearms_after_step_away_and_refires():
    st = AlertState.initial()
    # establish streak at t=0, present, "editor"
    s0 = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, s0, 0.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.stuck_window == "editor" and st.stuck_since == 0.0

    # 90 min later, still present, same window → fires once, disarms
    s1 = _sample(active_window="editor", idle_s=5.0)
    alerts, st = evaluate_alerts(st, s1, STUCK, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["window_stuck"]
    assert st.stuck_armed is False

    # step away (idle high), same window → recovery event: re-arm
    away = _sample(active_window="editor", idle_s=300.0)
    _, st = evaluate_alerts(st, away, 6000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.stuck_since is None
    assert st.stuck_armed is True

    # return present, same window → streak restarts at return time
    back = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, back, 6060.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert st.stuck_since == 6060.0

    # another full STUCK elapses, present, same window → fires AGAIN
    alerts, st = evaluate_alerts(st, back, 6060.0 + STUCK, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["window_stuck"]
    assert st.stuck_armed is False


# --- throttle + deferred-retry ---

def test_throttle_defers_second_candidate_not_drops():
    st = AlertState.initial()
    # battery critical + gpu hot in the SAME sample; last_fire_at=now-100 (within 900 throttle)
    s = _sample(battery_pct=12.0, battery_status="Discharging", gpus=[(50.0, 80.0)])
    st = AlertState(True, True, None, None, True, last_fire_at=9_900.0)
    alerts, st2 = evaluate_alerts(st, s, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    # within throttle window (100 < 900) → NOTHING fires this call
    assert alerts == []
    # both still armed (deferred, not dropped)
    assert st2.battery_armed is True and st2.gpu_armed is True
    # throttle window passes → battery (priority) fires; gpu deferred again same call
    alerts, st3 = evaluate_alerts(st2, s, 11_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["battery_critical"]
    assert st3.battery_armed is False and st3.gpu_armed is True
    # next call after throttle → gpu fires
    alerts, st4 = evaluate_alerts(st3, s, 12_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert [a.slug for a in alerts] == ["gpu_hot"]


# --- PulseObserver IO shell ---
#
# NOTE (queue-API adaptation): the brief's test sketch calls `q.drain_grouped()`
# synchronously and treats it as a flat `list[Perception]`. The real
# `PerceptionQueue.drain_grouped()` (src/dollos/mind/perception_queue.py) is
# `async` and returns `list[list[Perception]]` — one bucket per origin
# `data["channel_id"]` (spec §3.1 R2-C1). PulseMoment perceptions carry no
# channel_id, so they all land in a single bucket. `_drained()` below awaits
# drain_grouped() and flattens the buckets into a flat perception list,
# preserving order, so the assertions below check the same behavior the
# brief intended (one PulseMoment perception with data["concern"]/["detail"]).
#
# Second adaptation: `drain()` (which `drain_grouped()` wraps) blocks
# indefinitely when the queue is empty and `timeout_s` is None — there is no
# non-blocking "peek what's queued" mode. The no-emit assertion needs to
# observe an empty result without hanging forever, so `_drained()` passes a
# short `timeout_s` throughout; when perceptions are already queued (put()
# is synchronous/non-blocking) the underlying `queue.get()` resolves almost
# immediately regardless, so this doesn't change the emit-path assertions.


class _FakePulse:
    def __init__(self, sample):
        self._sample = sample

    def latest_sample(self):
        return self._sample


def _observer(sample):
    q = PerceptionQueue()
    obs = PulseObserver(
        system_pulse=_FakePulse(sample), queue=q,
        throttle_s=THROTTLE, window_stuck_s=STUCK,
    )
    return obs, q


async def _drained(q, timeout_s: float = 0.1):
    buckets = await q.drain_grouped(timeout_s=timeout_s)
    return [p for bucket in buckets for p in bucket]


@pytest.mark.asyncio
async def test_tick_emits_pulsemoment_on_battery_edge():
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    obs, q = _observer(s)
    obs._tick(10_000.0)
    perts = await _drained(q)
    assert len(perts) == 1
    p = perts[0]
    assert p.kind == "PulseMoment"
    assert p.data["concern"] == "battery_critical"
    assert "放電" in p.data["detail"]


@pytest.mark.asyncio
async def test_tick_no_emit_when_sample_none():
    obs, q = _observer(None)
    obs._tick(10_000.0)
    assert await _drained(q) == []


@pytest.mark.asyncio
async def test_tick_throttle_within_window_defers():
    s = _sample(battery_pct=12.0, battery_status="Discharging", gpus=[(50.0, 80.0)])
    obs, q = _observer(s)
    obs._tick(10_000.0)              # battery fires, gpu deferred (same-call throttle)
    first = await _drained(q)
    assert [p.data["concern"] for p in first] == ["battery_critical"]
    obs._tick(10_100.0)             # within 900s of last fire → nothing
    assert await _drained(q) == []
    obs._tick(11_000.0)            # throttle passed → gpu fires
    assert [p.data["concern"] for p in await _drained(q)] == ["gpu_hot"]


# --- severity field (Task 1) ---


def test_battery_alert_severity_critical():
    st = AlertState.initial()
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    alerts, _ = evaluate_alerts(st, s, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts[0].severity == "critical"


def test_gpu_alert_severity_critical():
    st = AlertState.initial()
    s = _sample(gpus=[(50.0, 80.0)])
    alerts, _ = evaluate_alerts(st, s, 10_000.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts[0].severity == "critical"


def test_window_stuck_alert_severity_advisory():
    st = AlertState.initial()
    s0 = _sample(active_window="editor", idle_s=5.0)
    _, st = evaluate_alerts(st, s0, 0.0, throttle_s=THROTTLE, window_stuck_s=STUCK)
    s1 = _sample(active_window="editor", idle_s=5.0)
    alerts, _ = evaluate_alerts(st, s1, STUCK, throttle_s=THROTTLE, window_stuck_s=STUCK)
    assert alerts[0].severity == "advisory"


@pytest.mark.asyncio
async def test_tick_puts_severity_in_data():
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    obs, q = _observer(s)
    obs._tick(10_000.0)
    perts = await _drained(q)
    p = perts[0]
    assert p.data["severity"] == "critical"
    assert p.data["concern"] == "battery_critical"
