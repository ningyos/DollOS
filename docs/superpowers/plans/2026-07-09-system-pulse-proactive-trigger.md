# System Pulse 主動觸發 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 當宿主機器跨過負向門檻(電池 critical / GPU 過熱 / 久卡同視窗)時,fire 一個 `PulseMoment` 自發回合喚醒 Doll,讓她可以主動出聲。

**Architecture:** 沿用既有 observer pattern。`SystemPulse`(已存在,純感測器)加一個 `latest_sample()` 存取器;新 `PulseObserver`(薄 IO 殼)讀樣本、呼叫純函式 `evaluate_alerts`(edge 偵測 + re-arm + throttle + deferred-retry)、把 `PulseMoment` 塞進 `PerceptionQueue`。`mind_loop` 加 `_is_pulse` 判定,收窄工具到 `PULSE_TOOLS` 但**不抑制發話**。

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest, uv。

## Global Constraints

- 全程 `cd` 到 repo root(`/home/progcat/Projects/DollOS`);測試用 `uv run pytest`。
- 分支已建:`system-pulse-proactive-trigger`(spec 已 commit 於此)。全程留在此分支。
- **No fallback**:任何來源缺失照既有 `SystemPulse` 慣例靜默省略,不造假值。
- 觸發政策的**唯一權威實作點**是純函式 `evaluate_alerts` —— observer 只做 IO。
- 發話抑制的唯一 chokepoint 是 `mind_loop._emit_sentence` 的 `if self._is_agenda or self._is_diary: return`。**絕不把 `_is_pulse` 加進這條**(發話 ON 是本 feature 目的)。
- 工具收窄鍵名(已核對 `MAIN_TOOLS`):`Recall` / `NoteMemory` / `MoodTool` = 各自 class name。
- 每個 task 結束都 commit,message 尾端附:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq
  ```
- Spec:`docs/superpowers/specs/2026-07-09-system-pulse-proactive-trigger-design.md`。決策預設 D1={battery_critical, gpu_hot, window_stuck}、D2=throttle 900s/stuck 5400s、D3=PULSE_TOOLS={Recall,NoteMemory,MoodTool}。

---

### Task 1: `SystemPulse.latest_sample()` 存取器

**Files:**
- Modify: `src/dollos/perception/system_pulse.py`(在 `latest_idle_s` 旁加方法)
- Test: `tests/test_system_pulse.py`(既有檔,追加)

**Interfaces:**
- Produces: `SystemPulse.latest_sample() -> PulseSample | None` — 回最後樣本;disabled / 無樣本 / 過期(>2× poll interval)回 `None`。鏡像既有 `latest_idle_s` 的 staleness 保護。

- [ ] **Step 1: Write the failing test**

在 `tests/test_system_pulse.py` 末尾追加:

```python
from datetime import datetime, timedelta
from dollos.perception.system_pulse import SystemPulse, PulseSample


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_system_pulse.py::test_latest_sample_returns_fresh -v`
Expected: FAIL with `AttributeError: 'SystemPulse' object has no attribute 'latest_sample'`

- [ ] **Step 3: Write minimal implementation**

在 `src/dollos/perception/system_pulse.py` 的 `latest_idle_s` 方法後面加:

```python
    def latest_sample(self) -> PulseSample | None:
        """The last fresh PulseSample, else None.

        None when: disabled / no sample yet / sample is stale (older than 2x
        poll interval). Mirrors ``latest_idle_s``'s staleness guard so a dead
        poll loop can't feed the PulseObserver an ancient reading.
        """
        if not self._enabled:
            return None
        s = self._last_sample
        if s is None:
            return None
        age = (datetime.now() - s.taken_at).total_seconds()
        if age > 2 * self._poll_interval_s:
            return None
        return s
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_system_pulse.py -v`
Expected: PASS(含既有測試,全綠)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/perception/system_pulse.py tests/test_system_pulse.py
git commit -m "feat(pulse): SystemPulse.latest_sample() accessor for PulseObserver

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 2: `SystemPulseConfig` 擴充(主動觸發旋鈕)

**Files:**
- Modify: `src/dollos/config.py:130-136`(`SystemPulseConfig`)
- Test: `tests/test_config.py`(既有檔,追加;若無此檔則建立)

**Interfaces:**
- Produces: `SystemPulseConfig.alerts_enabled: bool = True`、`alert_throttle_s: float = 900.0`、`window_stuck_s: float = 5400.0`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_config.py`(不存在就新建,頂部 `from dollos.config import SystemPulseConfig`)追加:

```python
from dollos.config import SystemPulseConfig


def test_system_pulse_alert_defaults():
    c = SystemPulseConfig()
    assert c.alerts_enabled is True
    assert c.alert_throttle_s == 900.0
    assert c.window_stuck_s == 5400.0


def test_system_pulse_alert_override():
    c = SystemPulseConfig(alerts_enabled=False, alert_throttle_s=60.0, window_stuck_s=120.0)
    assert c.alerts_enabled is False
    assert c.alert_throttle_s == 60.0
    assert c.window_stuck_s == 120.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_system_pulse_alert_defaults -v`
Expected: FAIL(`alerts_enabled` 不存在 → `AttributeError`,或因 `extra="forbid"` 使 override 測試建構失敗)

- [ ] **Step 3: Write minimal implementation**

把 `src/dollos/config.py` 的 `SystemPulseConfig` 改成:

```python
class SystemPulseConfig(BaseModel):
    """Proprioception poller — surfaces host vitals as a [Self pulse] block,
    and (alerts_enabled) wakes Doll via a PulseMoment when a vital crosses a
    negative/actionable/worsening threshold. See spec
    docs/superpowers/specs/2026-07-09-system-pulse-proactive-trigger-design.md.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    poll_interval_s: float = 60.0
    include_active_window: bool = True   # privacy opt-out

    # Proactive trigger (spec §6). alerts_enabled=False → PulseObserver never
    # starts; behavior is exactly today's passive-only [Self pulse] block.
    alerts_enabled: bool = True
    alert_throttle_s: float = 900.0      # global min interval between alerts (15 min)
    window_stuck_s: float = 5400.0       # same-window continuous-present threshold (90 min)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/config.py tests/test_config.py
git commit -m "feat(pulse): SystemPulseConfig alert knobs (alerts_enabled/throttle/window_stuck)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 3: 純函式 `evaluate_alerts` + `Alert` / `AlertState`

這是觸發政策的核心,測試面最大。純函式,無 mock 時鐘/佇列。

**Files:**
- Create: `src/dollos/mind/pulse_observer.py`(本 task 只放 dataclass + 純函式;Task 4 再加 `PulseObserver` class)
- Test: `tests/test_pulse_observer.py`(新建)

**Interfaces:**
- Produces:
  - `Alert(slug: str, text: str)`(frozen dataclass)
  - `AlertState`(frozen dataclass):`battery_armed: bool`、`gpu_armed: bool`、`stuck_window: str | None`、`stuck_since: float | None`、`stuck_armed: bool`、`last_fire_at: float`;`AlertState.initial() -> AlertState`(armed 全 True、stuck 全 None、last_fire_at=0.0)。
  - `evaluate_alerts(st: AlertState, sample: PulseSample, now: float, *, throttle_s: float, window_stuck_s: float) -> tuple[list[Alert], AlertState]`
- Consumes:`PulseSample`、`bucket_battery`、`bucket_gpu_temp`(from `dollos.perception.system_pulse`)。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_pulse_observer.py`:

```python
from datetime import datetime

from dollos.mind.pulse_observer import Alert, AlertState, evaluate_alerts
from dollos.perception.system_pulse import PulseSample

THROTTLE = 900.0
STUCK = 5400.0


def _sample(**kw) -> PulseSample:
    return PulseSample(taken_at=datetime.now(), **kw)


def _eval(st, sample, now, last_fire_at=0.0):
    # last_fire_at threaded via state; helper keeps tests terse
    st = AlertState(
        battery_armed=st.battery_armed, gpu_armed=st.gpu_armed,
        stuck_window=st.stuck_window, stuck_since=st.stuck_since,
        stuck_armed=st.stuck_armed, last_fire_at=last_fire_at,
    )
    return evaluate_alerts(st, sample, now, throttle_s=THROTTLE, window_stuck_s=STUCK)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pulse_observer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dollos.mind.pulse_observer'`

- [ ] **Step 3: Write minimal implementation**

新建 `src/dollos/mind/pulse_observer.py`:

```python
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
            # stepped away → streak broken AND re-armed. Step-away is the
            # recovery signal for window_stuck (symmetric to battery/gpu
            # re-arming on any not-bad tick); without re-arming here the rule
            # fires at most once per window title forever. (review-fixed)
            stuck_since = None
            stuck_armed = True
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pulse_observer.py -v`
Expected: PASS(全部案例綠)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/pulse_observer.py tests/test_pulse_observer.py
git commit -m "feat(pulse): evaluate_alerts pure policy (edge+re-arm+throttle+deferred-retry)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 4: `PulseObserver` IO 殼 + `PulseMoment` perception kind

**Files:**
- Modify: `src/dollos/mind/mind_state.py:82-88`(`Perception.kind` Literal 加 `"PulseMoment"`)
- Modify: `src/dollos/mind/pulse_observer.py`(加 `PulseObserver` class)
- Test: `tests/test_pulse_observer.py`(追加)

**Interfaces:**
- Consumes:`evaluate_alerts`/`AlertState`(Task 3)、`SystemPulse.latest_sample`(Task 1)、`Perception`/`PerceptionQueue`。
- Produces:`PulseObserver(*, system_pulse, queue, throttle_s, window_stuck_s, poll_interval_s=60.0)`;`._tick(now: float) -> None`(one evaluate+emit);`async run()`;`shutdown()`。emit 的 perception 是 `Perception(kind="PulseMoment", t=now, data={"concern": slug, "detail": text})`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_pulse_observer.py` 追加:

```python
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.pulse_observer import PulseObserver


class _FakePulse:
    def __init__(self, sample):
        self._sample = sample
    def latest_sample(self):
        return self._sample


def _observer(sample):
    q = PerceptionQueue()
    obs = PulseObserver(
        system_pulse=_FakePulse(sample), queue=q,
        throttle_s=900.0, window_stuck_s=5400.0,
    )
    return obs, q


def test_tick_emits_pulsemoment_on_battery_edge():
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    obs, q = _observer(s)
    obs._tick(10_000.0)
    perts = q.drain_grouped()
    assert len(perts) == 1
    p = perts[0]
    assert p.kind == "PulseMoment"
    assert p.data["concern"] == "battery_critical"
    assert "放電" in p.data["detail"]


def test_tick_no_emit_when_sample_none():
    obs, q = _observer(None)
    obs._tick(10_000.0)
    assert q.drain_grouped() == []


def test_tick_throttle_within_window_defers():
    s = _sample(battery_pct=12.0, battery_status="Discharging", gpus=[(50.0, 80.0)])
    obs, q = _observer(s)
    obs._tick(10_000.0)              # battery fires, gpu deferred (same-call throttle)
    first = q.drain_grouped()
    assert [p.data["concern"] for p in first] == ["battery_critical"]
    obs._tick(10_100.0)             # within 900s of last fire → nothing
    assert q.drain_grouped() == []
    obs._tick(11_000.0)            # throttle passed → gpu fires
    assert [p.data["concern"] for p in q.drain_grouped()] == ["gpu_hot"]
```

> 注意:`drain_grouped()` 回傳的是 perception list(見 `perception_queue.py`)。若其簽章/回傳型別與此不同(例如回 `list[Perception]` 或需先判空),依實際 API 調整取用方式 —— 先 `grep -n "def drain_grouped" src/dollos/mind/perception_queue.py` 核對。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pulse_observer.py::test_tick_emits_pulsemoment_on_battery_edge -v`
Expected: FAIL(`PulseObserver` 尚未定義 / `PulseMoment` 不在 Literal)

- [ ] **Step 3: Write minimal implementation**

(a) `src/dollos/mind/mind_state.py` 的 `Perception.kind` Literal 加 `"PulseMoment"`:

```python
    kind: Literal[
        "UserSpoke", "ToolResultArrived", "MonitorFired",
        "MonitorEnded", "ScheduledMoment", "Awoke", "ReflectionMoment",
        "Interrupted", "SafeModeEntered", "RepeatLoopDetected",
        "PersonaDriftDetected", "ChannelMessage", "BridgeDown", "McpDown",
        "AgendaMoment", "DiaryMoment", "PulseMoment",
    ]
```

(b) `src/dollos/mind/pulse_observer.py` 頂部 import 補上:

```python
import asyncio
import logging
import time
from dataclasses import dataclass, replace

from dollos.mind.mind_state import Perception
from dollos.mind.perception_queue import PerceptionQueue
```

> `logger = logging.getLogger(__name__)` 放在 import 後。

檔尾追加 `PulseObserver`:

```python
_POLL_INTERVAL_S = 60.0

logger = logging.getLogger(__name__)


class PulseObserver:
    """Thin IO shell around evaluate_alerts (spec §4.2). Mirrors AgendaObserver:
    poll SystemPulse, run the pure policy, put PulseMoment perceptions.
    """

    def __init__(
        self,
        *,
        system_pulse,
        queue: PerceptionQueue,
        throttle_s: float,
        window_stuck_s: float,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self._system_pulse = system_pulse
        self._queue = queue
        self._throttle_s = throttle_s
        self._window_stuck_s = window_stuck_s
        self._poll_interval_s = poll_interval_s
        self._state = AlertState.initial()
        self._shutdown = False

    def _tick(self, now: float) -> None:
        sample = self._system_pulse.latest_sample()
        if sample is None:
            return
        alerts, self._state = evaluate_alerts(
            self._state, sample, now,
            throttle_s=self._throttle_s, window_stuck_s=self._window_stuck_s,
        )
        for a in alerts:
            self._queue.put(Perception(
                kind="PulseMoment", t=now,
                data={"concern": a.slug, "detail": a.text},
            ))
            logger.info("PulseMoment fired: %s", a.slug)

    async def run(self) -> None:
        # boot grace: seed last_fire_at = now so a standing bad state at daemon
        # start doesn't fire instantly on every restart (mirrors AgendaObserver
        # not treating cold start as overdue). First alert eligible throttle_s
        # after boot.
        self._state = replace(self._state, last_fire_at=time.time())
        while not self._shutdown:
            await asyncio.sleep(self._poll_interval_s)
            try:
                self._tick(time.time())
            except Exception:
                logger.exception("pulse tick failed; continuing")

    def shutdown(self) -> None:
        self._shutdown = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pulse_observer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/pulse_observer.py src/dollos/mind/mind_state.py tests/test_pulse_observer.py
git commit -m "feat(pulse): PulseObserver IO shell + PulseMoment perception kind

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 5: `mind_loop` `_is_pulse` + `PULSE_TOOLS` 收窄(發話 ON)

**Files:**
- Modify: `src/dollos/tools.py:1233`(加 `PULSE_TOOLS`,在 `AGENDA_TOOLS` 旁)
- Modify: `src/dollos/mind/mind_loop.py`(import、`__init__` 加 `_is_pulse`、`_run_one_turn` 設 `_is_pulse`、`_active_tool_registry` 加分支)
- Test: `tests/test_mind_loop_pulse.py`(新建)

**Interfaces:**
- Consumes:`PULSE_TOOLS`(from tools)、`_active_tool_registry` 機制。
- Produces:`MindLoop._is_pulse: bool`;純 `PulseMoment` 批 → registry narrow 到 `PULSE_TOOLS`;co-batch UserSpoke → 完整 registry;發話**不**被抑制。

- [ ] **Step 1: Write the failing test**

先 `grep -n "def _active_tool_registry\|_is_agenda = \|self._is_agenda: bool" src/dollos/mind/mind_loop.py` 與看 `tests/` 內既有 agenda registry 測試(如 `test_mind_loop*agenda*`)當範本,對齊 `MindLoop` 建構所需的 fixture。新建 `tests/test_mind_loop_pulse.py`:

```python
"""PulseMoment turn: tools narrow to PULSE_TOOLS, speech NOT suppressed."""
from dollos.mind.mind_state import Perception
from dollos.tools import PULSE_TOOLS, MAIN_TOOLS


def _make_loop():
    # Reuse the same MindLoop construction helper the agenda registry tests use.
    # If a shared fixture/factory exists (e.g. tests/conftest.py make_mind_loop),
    # use it; otherwise mirror the minimal MindLoop(...) kwargs from the closest
    # existing test_mind_loop*.py. The loop needs a tool_registry built from
    # MAIN_TOOLS class names.
    from tests.helpers_mind_loop import make_mind_loop  # adjust import to actual helper
    return make_mind_loop()


def test_pulse_moment_narrows_to_pulse_tools():
    loop = _make_loop()
    loop._is_reflection = False
    loop._is_diary = False
    loop._is_agenda = False
    loop._has_user_spoke = False
    loop._is_pulse = True
    loop._ctx.origin_tier = "internal"
    reg = loop._active_tool_registry()
    assert set(reg.keys()) == set(PULSE_TOOLS)
    assert "Shell" not in reg and "SpawnWorkflow" not in reg


def test_pulse_cobatched_with_user_is_full_registry():
    loop = _make_loop()
    loop._is_reflection = False
    loop._is_diary = False
    loop._is_agenda = False
    loop._is_pulse = False        # co-batch → not a pure pulse turn
    loop._has_user_spoke = True
    loop._ctx.origin_tier = "internal"
    reg = loop._active_tool_registry()
    assert "Shell" in reg  # full registry, user request never narrowed
```

> 若無現成 `make_mind_loop` helper:直接看最靠近的既有 `tests/test_mind_loop*.py`(有測 `_is_agenda` registry 的那個)複製它建構 `MindLoop` 的方式。**不要**自己發明新 fixture 形狀。同時追加一個發話測試:仿既有 `_emit_sentence` 測試(如 `test_mind_loop_empty_speech_chunk.py` / agenda 的 speech-suppress 測試),斷言 `loop._is_pulse=True` 時 `_emit_sentence(sink, "hi")` **有**寫進 sink(對照 agenda/diary 是不寫)。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mind_loop_pulse.py -v`
Expected: FAIL(`PULSE_TOOLS` ImportError,或 `_is_pulse` AttributeError)

- [ ] **Step 3: Write minimal implementation**

(a) `src/dollos/tools.py`,在 `AGENDA_TOOLS` 定義(line 1233 附近)後加:

```python
# Pure PulseMoment turn (spec 2026-07-09 §5.3): cognition-only + speech ON.
# She can Recall/NoteMemory/adjust Mood and SPEAK UP, but no autonomous
# external action (no Shell/Workflow) on a self-wake. Speech is NOT gated by
# this set — it stays on because _is_pulse is deliberately absent from
# _emit_sentence's suppression (unlike AGENDA/DIARY).
PULSE_TOOLS: frozenset[str] = frozenset({"Recall", "NoteMemory", "MoodTool"})
```

(b) `src/dollos/mind/mind_loop.py` import：把 `AGENDA_TOOLS, DIARY_TOOLS` 那行 import 補進 `PULSE_TOOLS`(先 `grep -n "AGENDA_TOOLS" src/dollos/mind/mind_loop.py` 找到 import 行)。

(c) `__init__` 在 `self._is_diary: bool = False` 附近加:

```python
        # Pure PulseMoment turn flag (spec 2026-07-09 §5.2). Same "entire batch"
        # guard as _is_agenda/_is_diary. Unlike them, does NOT suppress speech.
        self._is_pulse: bool = False
```

(d) `_run_one_turn` 在設定 `self._is_diary`/`self._diary_in_batch` 之後加:

```python
        # Pure PulseMoment turn (spec 2026-07-09 §5.2): same pure-batch guard —
        # a PulseMoment co-batched with a live UserSpoke (or any other kind)
        # falls through to a normal turn, never narrowed.
        self._is_pulse = bool(perceptions) and all(
            p.kind == "PulseMoment" for p in perceptions
        )
```

(e) `_active_tool_registry`,在 `if self._is_agenda:` 分支**之後**、`if self._has_user_spoke:` 之前加:

```python
        if self._is_pulse:
            # Pure pulse turn (spec §5.3): narrow to PULSE_TOOLS (cognition +
            # speech). Reached only when origin_tier == "internal" and not
            # reflection/diary/agenda (all returned above) and the whole batch
            # is PulseMoment — a co-batched UserSpoke makes _is_pulse False and
            # falls through to the full-registry branch below. Speech is NOT
            # suppressed for this turn (see _emit_sentence — _is_pulse absent).
            return {
                n: c for n, c in self._tool_registry.items() if n in PULSE_TOOLS
            }
```

> **關鍵**:不要改 `_emit_sentence`。它的 `if self._is_agenda or self._is_diary: return` 保持原樣 —— `_is_pulse` 不加入,發話才會 ON。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mind_loop_pulse.py -v`
Then: `uv run pytest tests/ -k "mind_loop" -q`(確認未回歸既有 agenda/diary registry 測試)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/tools.py src/dollos/mind/mind_loop.py tests/test_mind_loop_pulse.py
git commit -m "feat(pulse): PulseMoment turn — PULSE_TOOLS narrow, speech stays ON

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 6: Kernel 接線

**Files:**
- Modify: `src/dollos/kernel.py`(import、`__init__` 建 observer(~line 597 旁)、`start` 起 task(~line 1248 旁)、`stop` 收 task(~line 1315 旁))
- Test: `tests/test_kernel_pulse_wiring.py`(新建,輕量;或併入既有 kernel 測試)

**Interfaces:**
- Consumes:`PulseObserver`(Task 4)、`settings.system_pulse.*`(Task 2)、`self.system_pulse`、`self._perception_queue`。

- [ ] **Step 1: Write the failing test**

先 `grep -n "system_pulse\|_agenda_observer\|_agenda_task\|_perception_queue" src/dollos/kernel.py` 對齊既有欄位名。新建 `tests/test_kernel_pulse_wiring.py`(輕量,只驗建構與 gating,不起真 event loop):

```python
"""Kernel wires PulseObserver; gated by alerts_enabled."""
# Mirror the construction style of the closest existing kernel test. The point
# is only: (1) when alerts_enabled, kernel has a _pulse_observer whose queue is
# the kernel's perception queue and whose throttle/window come from config;
# (2) alerts_enabled=False → no pulse task started.
#
# If kernel construction is heavy in tests, assert at the unit boundary instead:
# construct PulseObserver directly with settings values and check it shares the
# queue. Keep this test consistent with how other observers (agenda) are tested.
```

> 依既有 kernel 測試風格落實。若既有測試對 observer 只做「建構 + 欄位存在」層級的斷言,照做即可;不要為此新建重型 e2e。核心斷言:`kernel._pulse_observer` 存在、`alerts_enabled=False` 時 `kernel._pulse_task is None`。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kernel_pulse_wiring.py -v`
Expected: FAIL(`_pulse_observer` 不存在)

- [ ] **Step 3: Write minimal implementation**

(a) import(kernel.py 頂部,`from dollos.perception.system_pulse import SystemPulse` 附近):

```python
from dollos.mind.pulse_observer import PulseObserver
```

(b) `__init__`,在 `self._agenda_observer = AgendaObserver(...)` 之後加:

```python
        # PulseObserver — proactive host-vitals wake (spec 2026-07-09 §7),
        # mirrors AgendaObserver. Shares the perception queue; policy lives in
        # evaluate_alerts. Gated on start() by system_pulse.alerts_enabled.
        self._pulse_observer = PulseObserver(
            system_pulse=self.system_pulse,
            queue=self._perception_queue,
            throttle_s=settings.system_pulse.alert_throttle_s,
            window_stuck_s=settings.system_pulse.window_stuck_s,
        )
        self._pulse_task = None
```

> `self._pulse_task = None` 若與既有 `__init__` 的 task 欄位初始化風格不同(有些在別處初始化),依既有慣例放置。同時把 `settings` 存取對齊既有寫法(kernel 內既有 `settings.system_pulse.poll_interval_s` 用法)。

(c) `start`,在 `self.system_pulse.start()` 之後加:

```python
            # Proactive pulse alerts (spec §7). Gated separately from the passive
            # poller: alerts_enabled=False keeps today's passive-only behavior.
            if settings.system_pulse.alerts_enabled:
                self._pulse_task = asyncio.create_task(
                    self._pulse_observer.run(), name="pulse-observer"
                )
```

> 若 `start` 內取用的是 `self._settings`/區域 `settings`,對齊該作用域實際變數名(先看 `_agenda_task = asyncio.create_task(...)` 那段怎麼拿 settings)。

(d) `stop`,在既有 observer 收尾處(`_agenda_observer.shutdown()` 之類附近;若無,則在 cancel 其他 task 的區塊)加:

```python
            self._pulse_observer.shutdown()
            if self._pulse_task is not None and not self._pulse_task.done():
                self._pulse_task.cancel()
                try:
                    await self._pulse_task
                except (asyncio.CancelledError, Exception):
                    pass
```

> 對齊既有 `_agenda_task` / `_reflection_task` 的 cancel/await 收尾寫法(先 `grep -n "_agenda_task\|_reflection_task" src/dollos/kernel.py` 看它們怎麼收)。

- [ ] **Step 4: Run tests + full suite**

Run: `uv run pytest tests/test_kernel_pulse_wiring.py -v`
Then: `uv run pytest -q`(全套,確認零回歸)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel_pulse_wiring.py
git commit -m "feat(pulse): wire PulseObserver into kernel (gated by alerts_enabled)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

## Live Smoke(人工,實作後、merge 前 —— CI 跑不到)

依 `ref_weak_model_soft_mechanism_playbook`(prompt 管不住的語意必 live smoke):

1. `config.toml` 暫時把 `[system_pulse] alert_throttle_s = 30`、`window_stuck_s = 60` 調小以便觀察。
2. 起 daemon,讓它 idle。造一個觸發:
   - **battery**:若無電池,臨時在 `evaluate_alerts` 之外用一個 fake sample 灌 `battery_pct=12, status="Discharging"`(或跑筆電拔電);或
   - **gpu**:跑一個 GPU 負載讓溫度 >75°C;或
   - **window**:盯著同一視窗過 `window_stuck_s`。
3. 觀察:(a) log 有 `PulseMoment fired`;(b) Doll **真的開口**且內容合理(引用 `detail`);(c) 持續壞狀態**不**每 60s 洗版;(d) 復原後再壞會 re-arm 再響。
4. co-batch:觸發的同時對她講話 → 確認落回正常回合(完整工具、正常對話),pulse 不搶戲。
5. 關閉:`alerts_enabled = false` → 確認 observer 不啟動、行為回到今天。
6. 還原 `config.toml` 的 throttle/window 值。

---

## Self-Review(plan 對 spec)

**1. Spec coverage:**
- §3 觸發原則(D1 三規則)→ Task 3 `evaluate_alerts`。✅
- §4.1 `latest_sample` → Task 1。✅
- §4.2 PulseObserver + throttle/deferred-retry → Task 3(政策)+ Task 4(IO 殼)。✅
- §4.3 純函式 → Task 3。✅
- §5.1 PulseMoment kind → Task 4。✅
- §5.2 `_is_pulse` pure-batch → Task 5。✅
- §5.3 PULSE_TOOLS 收窄 → Task 5。✅
- §5.4 發話 ON(不動 `_emit_sentence`)→ Task 5 明列「不要改」。✅
- §5.5 data.detail 自足 → Task 3 的 Alert.text + Task 4 的 data payload。✅
- §6 config → Task 2。✅
- §7 kernel 接線 → Task 6。✅
- §8 測試 → 各 task 內含;§9 live smoke → 專節。✅

**2. Placeholder scan:** 無 TBD/TODO;每個 code step 都有完整可貼上的實作。Task 5/6 的測試步驟因需對齊既有 fixture 形狀而給了「照既有測試複製」指引 —— 這是為避免發明錯誤 fixture,非佔位;實作者仍有明確斷言目標(narrow 到 PULSE_TOOLS / `_pulse_observer` 存在 + gating)。

**3. Type consistency:** `evaluate_alerts` 簽章、`AlertState` 六欄位、`Alert(slug,text)`、`PulseObserver(*, system_pulse, queue, throttle_s, window_stuck_s, poll_interval_s)`、`data={"concern","detail"}`、`PULSE_TOOLS` 鍵名(`Recall`/`NoteMemory`/`MoodTool`,已核對 `MAIN_TOOLS`)—— 跨 task 一致。

**已知限制(v1,非阻塞)**:`PulseMoment` 與 `AgendaMoment` 在同一 drain 窗 co-batch 時(兩者皆 origin-less internal),`_is_pulse`/`_is_agenda` 皆為 False → 落到完整 registry(既有的 self-wake co-batch fall-through 行為,非本 feature 新引入)。罕見 race;若實測發現困擾,另開 issue 收斂。
