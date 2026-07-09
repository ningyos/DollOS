"""PulseObserver — turns SystemPulse readings into proactive PulseMoment wakes.

The passive `[Self pulse]` block (system_pulse.py) only informs turns that
already happen. This module adds the *active* half (spec 2026-07-09): when a
vital crosses a negative/actionable/worsening threshold, fire a PulseMoment so
Doll wakes and can choose to speak up.

`evaluate_alerts` is the single authoritative policy point — a PURE function
(edge-detect + re-arm-on-recovery + throttle + deferred-retry). `PulseObserver`
(Task 4) is a thin IO shell around it. Keeping policy pure means the trigger
behavior is 100% testable without mocking clocks, queues, or subprocesses.
"""
from __future__ import annotations

from dataclasses import dataclass

from dollos.perception.system_pulse import PulseSample, bucket_battery, bucket_gpu_temp

_PRESENT_IDLE_S = 60.0  # idle_s below this == user actively present (matches bucket_idle "present")


@dataclass(frozen=True)
class Alert:
    slug: str   # "battery_critical" | "gpu_hot" | "window_stuck"
    text: str   # self-contained zh description → PulseMoment.data.detail


@dataclass(frozen=True)
class AlertState:
    battery_armed: bool
    gpu_armed: bool
    stuck_window: str | None
    stuck_since: float | None
    stuck_armed: bool
    last_fire_at: float

    @staticmethod
    def initial() -> "AlertState":
        return AlertState(
            battery_armed=True,
            gpu_armed=True,
            stuck_window=None,
            stuck_since=None,
            stuck_armed=True,
            last_fire_at=0.0,
        )


def evaluate_alerts(
    st: AlertState,
    sample: PulseSample,
    now: float,
    *,
    throttle_s: float,
    window_stuck_s: float,
) -> tuple[list[Alert], AlertState]:
    """Pure policy. Returns (emitted_alerts, new_state).

    A rule emits only when it is in a bad bucket AND armed AND the global
    throttle window has elapsed. A throttled candidate does NOT consume its
    arm (deferred-retry) — it stays armed and retries next call, so a bad
    condition coinciding with the throttle window is delayed, never dropped.
    Arm is consumed only on an actual emit; re-armed only when the sample
    shows recovery.
    """
    battery_armed = st.battery_armed
    gpu_armed = st.gpu_armed
    stuck_window = st.stuck_window
    stuck_since = st.stuck_since
    stuck_armed = st.stuck_armed
    last_fire_at = st.last_fire_at

    # --- battery_critical: bucket critical (<15%) AND discharging ---
    battery_bad = False
    if sample.battery_pct is not None:
        crit = bucket_battery(sample.battery_pct) == "critical"
        discharging = (sample.battery_status or "").lower() == "discharging"
        battery_bad = crit and discharging
    if not battery_bad:
        battery_armed = True  # recovered / not applicable → re-arm

    # --- gpu_hot: hottest GPU temp bucket == hot (>75C) ---
    gpu_bad = False
    hottest = None
    if sample.gpus:
        hottest = max(t for _, t in sample.gpus)
        gpu_bad = bucket_gpu_temp(hottest) == "hot"
    if not gpu_bad:
        gpu_armed = True

    # --- window_stuck: same active_window, continuous present streak >= threshold ---
    stuck_bad = False
    present = sample.idle_s is not None and sample.idle_s < _PRESENT_IDLE_S
    win = sample.active_window
    if win is None:
        # window tracking off / unavailable → reset accumulator
        stuck_window = None
        stuck_since = None
        stuck_armed = True
    elif win != stuck_window:
        # window changed → new streak + re-arm
        stuck_window = win
        stuck_since = now if present else None
        stuck_armed = True
    else:
        # same window
        if not present:
            stuck_since = None  # stepped away → streak broken (continuous-present model)
            stuck_armed = True  # stepping away IS the recovery event → re-arm
        elif stuck_since is None:
            stuck_since = now   # returned / first present tick → start streak
        elif (now - stuck_since) >= window_stuck_s:
            stuck_bad = True

    # --- collect candidates in priority order (battery > gpu > window) ---
    candidates: list[tuple[str, Alert]] = []
    if battery_bad and battery_armed:
        candidates.append(("battery", Alert(
            slug="battery_critical",
            text=f"電量掉到 {sample.battery_pct:.0f}% 而且在放電",
        )))
    if gpu_bad and gpu_armed:
        candidates.append(("gpu", Alert(
            slug="gpu_hot",
            text=f"GPU 溫度到 {hottest:.0f}°C,燙",
        )))
    if stuck_bad and stuck_armed:
        mins = int((now - stuck_since) / 60)
        candidates.append(("window", Alert(
            slug="window_stuck",
            text=f"盯著「{stuck_window}」連續 {mins} 分鐘沒換",
        )))

    # --- throttle with deferred-retry; consume arm only on emit ---
    emitted: list[Alert] = []
    for rule, alert in candidates:
        if (now - last_fire_at) < throttle_s:
            break  # throttled — leave arm intact, retry next call
        emitted.append(alert)
        last_fire_at = now
        if rule == "battery":
            battery_armed = False
        elif rule == "gpu":
            gpu_armed = False
        elif rule == "window":
            stuck_armed = False

    new_state = AlertState(
        battery_armed=battery_armed,
        gpu_armed=gpu_armed,
        stuck_window=stuck_window,
        stuck_since=stuck_since,
        stuck_armed=stuck_armed,
        last_fire_at=last_fire_at,
    )
    return emitted, new_state
