from datetime import datetime

from dollos.mind.pulse_observer import Alert, AlertState, evaluate_alerts
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
