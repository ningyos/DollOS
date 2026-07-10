# System Pulse 臨場感調校 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Doll 在 critical pulse 喚醒時更可靠地主動出聲(仍是她的選擇),advisory 維持安靜傾向,喚醒事件能進日記反思。

**Architecture:** `Alert` 加 `severity`(critical/advisory),一路帶進 `PulseMoment.data`。純 `_is_pulse` 回合渲染一個描述性 `[Body signal]` 框架 block(仿 `[External situation]` caller-gated 前例,放 `[Decision time]` 前),依嚴重度給不同傾向(critical→傾向出聲、advisory→傾向安靜),**描述非命令**(Self-First)。`PulseMoment` 接進 `action_phrase_for_perception` → diary action-log。

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest, uv。

## Global Constraints

- `cd /home/progcat/Projects/DollOS`;測試 `uv run pytest`。分支已建 `system-pulse-presence-tuning`(spec 已 commit 於此)。全程留此分支。
- **軟槓桿,非命令**(user 拍板 A):`[Body signal]` 是描述性框架,保留她沉默的自由。**不做**程式強制發話。
- **不動**:`_emit_sentence`(發話仍 ON)、`evaluate_alerts` 的 throttle/edge/re-arm 政策、PulseObserver 生命週期。
- **嚴重度分派**(exact):`battery_critical`→`"critical"`、`gpu_hot`→`"critical"`、`window_stuck`→`"advisory"`。
- **`[Body signal]` 措辭**(exact,D1 已核可):
  - critical:`你因為身體狀況醒過來:{details}。這種事主人多半會想知道 —— 想說就跟他講一聲,不想說就自己記著。你決定。`
  - advisory:`你注意到:{details}。不緊急 —— 想順口提一句或默默記著都行。`
- **gating**:`[Body signal]` 只在**純 `_is_pulse` 回合**渲染(co-batch UserSpoke → `_is_pulse` False → 不渲染)。
- **位置**(D3):`[Body signal]` 插在 `[Decision time]` **之前**。
- 每 task commit,message 尾端附:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq
  ```
- Spec:`docs/superpowers/specs/2026-07-09-system-pulse-presence-tuning-design.md`。

---

### Task 1: `Alert.severity` + 帶進 `PulseMoment.data`

**Files:**
- Modify: `src/dollos/mind/pulse_observer.py`(`Alert` dataclass + `evaluate_alerts` 三處 Alert 建構 + `PulseObserver._tick` 的 data payload)
- Test: `tests/test_pulse_observer.py`(追加)

**Interfaces:**
- Produces: `Alert(slug: str, text: str, severity: str)`;`severity ∈ {"critical","advisory"}`。`PulseObserver._tick` 產生的 `Perception.data` 新增 `"severity"` 鍵(與既有 `concern`/`detail` 並列)。

- [ ] **Step 1: Write the failing test**

在 `tests/test_pulse_observer.py` 追加:

```python
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


def test_tick_puts_severity_in_data():
    s = _sample(battery_pct=12.0, battery_status="Discharging")
    obs, q = _observer(s)
    obs._tick(10_000.0)
    p = q.drain_grouped()[0]  # helper flattens; adapt to your queue API as in existing tests
    assert p.data["severity"] == "critical"
    assert p.data["concern"] == "battery_critical"
```

> `drain_grouped()` 用法對齊本檔既有 test 的取用方式(Task 4 of the prior plan 已確立 async/bucketed;若本檔有 `_drained()`/`_observer()` helper 就用它)。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pulse_observer.py::test_battery_alert_severity_critical -v`
Expected: FAIL(`Alert.__init__() missing ... 'severity'` 或 `AttributeError: severity`)

- [ ] **Step 3: Write minimal implementation**

在 `src/dollos/mind/pulse_observer.py`:

(a) `Alert` dataclass 加 `severity`:
```python
@dataclass(frozen=True)
class Alert:
    slug: str        # "battery_critical" | "gpu_hot" | "window_stuck"
    text: str        # self-contained zh description → PulseMoment.data.detail
    severity: str    # "critical" | "advisory" (spec §3.1)
```

(b) `evaluate_alerts` 三處 candidate 建構加 `severity`:
```python
    if battery_bad and battery_armed:
        candidates.append(("battery", Alert(
            slug="battery_critical",
            text=f"電量掉到 {sample.battery_pct:.0f}% 而且在放電",
            severity="critical",
        )))
    if gpu_bad and gpu_armed:
        candidates.append(("gpu", Alert(
            slug="gpu_hot",
            text=f"GPU 溫度到 {hottest:.0f}°C,燙",
            severity="critical",
        )))
    if stuck_bad and stuck_armed:
        mins = int((now - stuck_since) / 60)
        candidates.append(("window", Alert(
            slug="window_stuck",
            text=f"盯著「{stuck_window}」連續 {mins} 分鐘沒換",
            severity="advisory",
        )))
```

(c) `PulseObserver._tick` 的 data payload 加 `severity`:
```python
        for a in alerts:
            self._queue.put(Perception(
                kind="PulseMoment", t=now,
                data={"concern": a.slug, "detail": a.text, "severity": a.severity},
            ))
            logger.info("PulseMoment fired: %s (%s)", a.slug, a.severity)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pulse_observer.py -v`
Expected: PASS(既有測試不受影響 —— severity 是新增欄位)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/pulse_observer.py tests/test_pulse_observer.py
git commit -m "feat(pulse): Alert.severity (critical/advisory) carried into PulseMoment.data

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 2: `render_body_signal` 純函式(措辭 + severity→wording)

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`(加 `render_body_signal`,放在 `_render_perceptions` 等 render helper 附近)
- Test: `tests/test_mind_prompt.py`(追加)

**Interfaces:**
- Produces: `render_body_signal(wakes: list[tuple[str, str]]) -> str | None`。`wakes` = `[(severity, detail), ...]`。任一 severity=="critical" → critical 措辭,否則 advisory。空 list → `None`。回傳含 `[Body signal]` 標頭的 pre-rendered 字串(仿 `pulse_block` 自帶標頭)。

- [ ] **Step 1: Write the failing test**

在 `tests/test_mind_prompt.py` 追加:

```python
from dollos.mind.mind_prompt import render_body_signal


def test_body_signal_critical_wording():
    out = render_body_signal([("critical", "電量掉到 12% 而且在放電")])
    assert out is not None
    assert out.startswith("[Body signal]")
    assert "電量掉到 12% 而且在放電" in out
    assert "主人多半會想知道" in out          # critical lean-to-surface phrasing
    assert "不緊急" not in out


def test_body_signal_advisory_wording():
    out = render_body_signal([("advisory", "盯著「editor」連續 118 分鐘沒換")])
    assert out is not None
    assert "不緊急" in out                    # advisory lean-to-restraint phrasing
    assert "主人多半會想知道" not in out
    assert "連續 118 分鐘沒換" in out


def test_body_signal_mixed_takes_critical():
    out = render_body_signal([("advisory", "卡視窗"), ("critical", "電量低")])
    assert "主人多半會想知道" in out          # any critical → critical framing
    assert "卡視窗" in out and "電量低" in out


def test_body_signal_empty_returns_none():
    assert render_body_signal([]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mind_prompt.py::test_body_signal_critical_wording -v`
Expected: FAIL(`ImportError: cannot import name 'render_body_signal'`)

- [ ] **Step 3: Write minimal implementation**

在 `src/dollos/mind/mind_prompt.py`(render helper 群附近)加:

```python
def render_body_signal(wakes: list[tuple[str, str]]) -> str | None:
    """The [Body signal] framing block for a pure PulseMoment wake turn
    (spec 2026-07-09 presence-tuning §3.2). Descriptive, NOT a command
    (Self-First): it names WHY she woke + a severity-tuned lean, and leaves
    the choice to speak-or-stay-quiet to her.

    ``wakes`` = ``[(severity, detail), ...]`` from this turn's PulseMoment
    perceptions. Any ``severity == "critical"`` → critical framing (lean to
    surface); else advisory framing (lean to restraint). Empty → None (not a
    pulse turn / no wakes). Returns a pre-rendered string incl. the
    ``[Body signal]`` header (mirrors ``pulse_block``).
    """
    if not wakes:
        return None
    details = "；".join(d for _, d in wakes if d)
    is_critical = any(sev == "critical" for sev, _ in wakes)
    if is_critical:
        body = (
            f"你因為身體狀況醒過來:{details}。這種事主人多半會想知道 —— "
            "想說就跟他講一聲,不想說就自己記著。你決定。"
        )
    else:
        body = f"你注意到:{details}。不緊急 —— 想順口提一句或默默記著都行。"
    return "[Body signal]\n" + body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mind_prompt.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_prompt.py tests/test_mind_prompt.py
git commit -m "feat(pulse): render_body_signal — severity-tuned descriptive wake framing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 3: 接線 —— `render_mind` `body_signal_block` 參數 + mind_loop 純 pulse 回合傳入

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`(`render_mind` 加 `body_signal_block` 參數 + 插在 `[Decision time]` 前)
- Modify: `src/dollos/mind/mind_loop.py`(import `render_body_signal`;純 pulse 回合抽 wakes 建 block;render_mind 呼叫傳入)
- Test: `tests/test_mind_loop_pulse.py`(追加)

**Interfaces:**
- Consumes: `render_body_signal`(Task 2)、`PulseMoment.data["severity"]`/`["detail"]`(Task 1)。
- Produces: `render_mind(..., body_signal_block: str | None = None)`;純 `_is_pulse` 回合的 prompt 含 `[Body signal]`(在 `[Decision time]` 前);非純 pulse 回合不含。

- [ ] **Step 1: Write the failing test**

在 `tests/test_mind_loop_pulse.py` 追加(沿用本檔既有 `make_mindloop` + 真 `_run_one_turn` 驅動風格,對齊既有 registry/speech 測試):

```python
async def _render_prompt_for_batch(ml, perceptions):
    """Drive one turn and capture the rendered prompt via the same seam the
    existing pulse tests use. If this file already has a helper that captures
    the rendered prompt (grep for 'render_mind' / a monkeypatched capture),
    reuse it; otherwise monkeypatch dollos.mind.mind_loop.render_mind to
    record its kwargs and assert on body_signal_block."""
    ...


def test_pure_pulse_turn_has_body_signal_block(...):
    # Build a pure PulseMoment batch with a critical severity in data, drive one
    # turn, and assert the rendered prompt contains "[Body signal]" and the
    # critical phrasing "主人多半會想知道".
    ...


def test_cobatch_userspoke_has_no_body_signal_block(...):
    # PulseMoment + UserSpoke co-batch → _is_pulse False → rendered prompt does
    # NOT contain "[Body signal]".
    ...
```

> **實作者**:先讀本檔既有測試怎麼觀察 render 結果(它們已能斷言 `_active_tool_registry`;grep 本檔 `render_mind`/monkeypatch/prompt 觀察 seam)。最簡穩的作法:`monkeypatch.setattr("dollos.mind.mind_loop.render_mind", capture)` 錄下 kwargs,斷言 `kwargs["body_signal_block"]` 在純 pulse 回合非 None 且含 critical 措辭、co-batch 回合為 None。用真 `PulseMoment` perception(`data={"concern":"battery_critical","detail":"電量掉到 12% 而且在放電","severity":"critical"}`)。不要發明新 MindLoop 形狀。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mind_loop_pulse.py -v`
Expected: FAIL(`render_mind() got an unexpected keyword argument 'body_signal_block'`,或 body_signal_block 為 None)

- [ ] **Step 3: Write minimal implementation**

(a) `src/dollos/mind/mind_prompt.py` `render_mind` 簽名加參數(在 `cognition_block` 附近):
```python
    cognition_block: str | None = None,
    body_signal_block: str | None = None,
```
docstring 補一句:`body_signal_block` — pre-rendered `[Body signal]` from `render_body_signal()`, only on pure PulseMoment turns; inserted right before `[Decision time]`。

在 `[Decision time]` 之前插入(找到組 `[Decision time]` 的那段;它前面可能有 `[External situation]`):
```python
    if body_signal_block:
        blocks.extend([body_signal_block, ""])
    blocks.extend([
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
    ])
```

(b) `src/dollos/mind/mind_loop.py`:
- import:把 `render_mind` 那行 import 補上 `render_body_signal`(`from dollos.mind.mind_prompt import ... render_mind, render_body_signal` — 先 grep 該 import 行對齊)。
- 在 `pulse_block`/`cognition_block` 計算附近(~609-620,`perceptions` 在 scope)加:
```python
        body_signal_block: str | None = None
        if self._is_pulse:
            wakes = [
                (
                    (p.data or {}).get("severity", "advisory"),
                    (p.data or {}).get("detail", ""),
                )
                for p in perceptions
                if p.kind == "PulseMoment"
            ]
            body_signal_block = render_body_signal(wakes)
```
- `render_mind(...)` 呼叫加一行參數(在 `cognition_block=cognition_block,` 旁):
```python
                body_signal_block=body_signal_block,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mind_loop_pulse.py -v`
Then: `uv run pytest tests/ -k "mind_prompt or mind_loop" -q`(無回歸)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_prompt.py src/dollos/mind/mind_loop.py tests/test_mind_loop_pulse.py
git commit -m "feat(pulse): wire [Body signal] framing into pure PulseMoment turns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 4: Diary 整合 —— `PulseMoment` → action-log

**Files:**
- Modify: `src/dollos/mind/action_log.py`(`action_phrase_for_perception` 加 `PulseMoment` case)
- Test: `tests/`(既有 action_log 測試檔;`grep -rln "action_phrase_for_perception" tests/` 找,無則新建 `tests/test_action_log.py`)

**Interfaces:**
- Produces: `action_phrase_for_perception("PulseMoment", data)` → 非 None 的世界事件句。

- [ ] **Step 1: Write the failing test**

在既有 action_log 測試檔(或新建)追加:

```python
from dollos.mind.action_log import action_phrase_for_perception


def test_pulse_moment_action_phrase():
    phrase = action_phrase_for_perception(
        "PulseMoment",
        {"concern": "battery_critical", "detail": "電量掉到 12% 而且在放電", "severity": "critical"},
    )
    assert phrase is not None
    assert "電量掉到 12% 而且在放電" in phrase


def test_non_pulse_kind_still_none():
    # a kind action_phrase_for_perception doesn't handle → still None (unchanged)
    assert action_phrase_for_perception("ReflectionMoment", {}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_action_log.py::test_pulse_moment_action_phrase -v`(或實際檔名)
Expected: FAIL(回 None → assertion error)

- [ ] **Step 3: Write minimal implementation**

在 `src/dollos/mind/action_log.py` 的 `action_phrase_for_perception`,`return None` 之前加(用既有 `_clip` helper,仿 ToolResult/Monitor 那幾條):
```python
    if kind == "PulseMoment":
        return f"身體狀況:{_clip(d.get('detail', ''), 80)}"
```

> 無 origin-gating 疑慮:action-log 世界事件結構上 owner/internal-only(`mind_loop.py:445-451` 註解),`PulseMoment` daemon-internal/origin-less,與既有 ToolResult/Monitor/BridgeDown 同類。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_action_log.py -v`(或實際檔名)
Then: `uv run pytest -q`(全套,零回歸)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/action_log.py tests/test_action_log.py
git commit -m "feat(pulse): PulseMoment wake → diary action-log (身體狀況)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

## Live Smoke(人工,實作後、merge 前 —— CI 跑不到)

依 `ref_weak_model_soft_mechanism_playbook`(prompt 軟機制必 live-smoke):

1. 用上一 feature 的 smoke harness(真 kernel + 真 LLM@8001 + scratch data.root),注入純 `PulseMoment` 批次(`data` 含 `severity`)。
2. **critical 出聲率**:battery-critical(severity=critical)喚醒重跑 ≥5 次,對照調校前(基準 1/3),看含 `[Body signal]` critical 框架後出聲率是否明顯提高。
3. **advisory 克制**:window_stuck(severity=advisory)喚醒數次,確認她**仍傾向安靜**(框架不該把 advisory 也逼成話癆)。
4. **co-batch**:PulseMoment + UserSpoke 同批 → 確認 prompt **無** `[Body signal]`、落正常回合。
5. **diary**:確認喚醒事件以「身體狀況:…」進 `[Today's log]`。
6. dump 一次純 pulse 回合的完整 prompt,肉眼確認 `[Body signal]` 在 `[Decision time]` 前、措辭正確。

---

## Self-Review(plan 對 spec)

**1. Spec coverage:**
- §3.1 severity(battery/gpu=critical、window=advisory)+ 帶進 data → Task 1。✅
- §3.2 `[Body signal]` 描述性框架 + severity→wording + gating 純 pulse + 位置 `[Decision time]` 前 → Task 2(措辭)+ Task 3(gating/位置/接線)。✅
- §3.3 diary(`action_phrase_for_perception` + 無 origin-gating 疑慮)→ Task 4。✅
- §4 資料流 → Task 1+3 串起。✅
- §5 測試 → 各 task 內含;live smoke 專節(§5 對照調校前出聲率)。✅
- §6 D1 措辭/D2 gpu=critical/D3 位置 → 全部落進 Global Constraints 的 exact 值。✅

**2. Placeholder scan:** Task 3 的測試步驟給「對齊既有觀察 seam / monkeypatch render_mind」指引(非佔位 —— 為避免發明錯 fixture;斷言目標明確:body_signal_block 純 pulse 非 None+critical 措辭、co-batch None)。其餘 code step 均完整。

**3. Type consistency:** `Alert(slug,text,severity)`、`PulseMoment.data{concern,detail,severity}`、`render_body_signal(wakes: list[tuple[str,str]]) -> str|None`、`render_mind(..., body_signal_block: str|None=None)`、措辭字串(「主人多半會想知道」/「不緊急」)跨 task 一致。
