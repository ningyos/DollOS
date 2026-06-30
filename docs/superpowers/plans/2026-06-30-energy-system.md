# B3: Energy System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 給 Doll 一個客觀的精力節律——真正做認知工作時消耗、閒置休息時回復,注入 prompt 讓回應風格隨之 emerge。

**Architecture:** `MindState.energy` (0-1 float)。消耗在 `MindLoop.iterate`(只在本 turn 產生 output 時)。回復在 `ConsolidationTrigger.run()` 現成 5s poll loop(idle-triggered,基準 `last_user_at`,脫鉤 consolidation target)。注入 `[Mind state]` 客觀描述(無感受詞)。軟性影響,不硬限制、不自動改 Mood。

**Tech Stack:** Python asyncio / pytest。沿用 B1 的 `_turn_speech`、B2 的 `ConsolidationTrigger`。

## Global Constraints（逐字自 spec）

- **回復脫鉤 consolidation 成敗**:idle-triggered,`consolidation target=None` 時仍回復(否則 dead state)。
- **消耗只算認知工作**:本 turn 有 speech 或 tool call 才扣;被動 monitor/schedule tick(Doll 只 Idle)不扣。
- **回復閒置基準 = `last_user_at`**(非 `max(last_user_at,last_iter_at)`,避免 monitor 餵死)。
- **energy 不自動改 Mood、不禁工具、不改 LLM 參數**(軟性,Self-First)。
- **bucket 文字無感受詞**(不寫「累」;只客觀區間 + 可選機制式 gloss)。
- **clamp [0,1]** 消耗下界、回復上界、load 時。
- **No-fallback**;TDD;`uv run pytest <path> -v`,repo root。

## File Structure

- Modify `src/dollos/mind/mind_state.py` — `energy`/`last_energy_restore_at` 欄位 + save/load 顯式三處 + load clamp。
- Modify `src/dollos/config.py` — `[energy]` Settings。
- Modify `src/dollos/mind/mind_loop.py` — 消耗 + `_turn_had_tool` flag + `__init__` signature。
- Modify `src/dollos/mind/consolidation.py` — 回復(poll loop)+ `__init__` signature。
- Modify `src/dollos/mind/mind_prompt.py` — energy 行注入(`render_mind` + `_render_mindstate`)。
- Modify `src/dollos/kernel.py` — 三處注入(MindLoop / ConsolidationTrigger / render_mind energy_line)。
- Test: `tests/test_energy.py`(新)、`tests/test_mind_state.py`、`tests/test_config.py`。

---

### Task 1: MindState energy 欄位 + load clamp

**Files:** Modify `mind_state.py`(dataclass + save dict + load 建構子 + load clamp);Test `tests/test_mind_state.py`

- [ ] **Step 1: 失敗測試**

```python
def test_energy_round_trip_and_clamp(tmp_path):
    from dollos.mind.mind_state import MindState, save_state, load_state
    import json
    s = MindState(); s.energy = 0.4; s.last_energy_restore_at = 12.0
    p = tmp_path / "s.json"; assert save_state(s, p)
    assert load_state(p).energy == 0.4
    assert load_state(p).last_energy_restore_at == 12.0
    # out-of-range clamps on load
    data = json.loads(p.read_text()); data["energy"] = 1.7; p.write_text(json.dumps(data))
    assert load_state(p).energy == 1.0
    data["energy"] = -0.5; p.write_text(json.dumps(data))
    assert load_state(p).energy == 0.0
```

- [ ] **Step 2-4:** 跑失敗 → 實作 → 通過。dataclass 加 `energy: float = 1.0`、`last_energy_restore_at: float = 0.0`;save dict 加兩 key;load 建構子加 `last_energy_restore_at=data.get("last_energy_restore_at", 0.0)` 與 **clamp**:`energy=min(1.0, max(0.0, float(data.get("energy", 1.0))))`。
- [ ] **Step 5: Commit** `feat(memory): B3 MindState energy fields + load clamp`

---

### Task 2: config [energy]

**Files:** Modify `config.py`;Test `tests/test_config.py`

- [ ] **Step 1: 失敗測試**

```python
def test_energy_config_defaults():
    from dollos.config import Settings
    c = Settings().energy
    assert c.enabled is True and c.cost_per_turn == 0.05
    assert c.restore_per_tick == 0.05 and c.idle_threshold_s == 600 and c.restore_debounce_s == 300
```

- [ ] **Step 2-4:** 仿 `ConsolidationConfig` 加 `EnergyConfig`(`enabled:bool=True`、`cost_per_turn:float=0.05`、`restore_per_tick:float=0.05`、`idle_threshold_s:int=600`、`restore_debounce_s:int=300`)+ 掛 `Settings.energy`。
- [ ] **Step 5: Commit** `feat(memory): B3 [energy] config`

---

### Task 3: 消耗（iterate，只算認知工作）

**Files:** Modify `mind_loop.py`(`__init__` signature、`iterate` 消耗、`_turn_had_tool` flag);Test `tests/test_energy.py`(新)

**Interfaces:** `MindLoop.__init__` 增 `energy_enabled: bool = False`、`cost_per_turn: float = 0.05`。

- [ ] **Step 1: 失敗測試**(沿用 `tests/_dispatcher_helpers` + `tests/test_mind_loop._FakeLLM`)

```python
@pytest.mark.asyncio
async def test_energy_consumed_on_speech_turn(tmp_path):
    # MindLoop(energy_enabled=True, cost_per_turn=0.1); UserSpoke + speak stream
    # after iterate(): state.energy == 0.9
@pytest.mark.asyncio
async def test_energy_not_consumed_on_passive_turn(tmp_path):
    # energy_enabled=True; a ScheduledMoment turn where Doll says nothing
    # (stream = think-only, no speech, no tool) → energy unchanged (1.0)
```

- [ ] **Step 2-4:** `__init__` 存 `self._energy_enabled`/`self._cost_per_turn`、init `self._turn_had_tool = False`。`iterate` 清 `_turn_speech` 處旁清 `self._turn_had_tool = False`。tool dispatch 點(`_handle_stream_event` 的 ToolCallReady 分支)set `self._turn_had_tool = True`。消耗(早退之後、`iter_count += 1` 旁、finally 外):
```python
        produced = bool(self._turn_speech) or self._turn_had_tool
        if self._energy_enabled and produced:
            self._state.energy = max(0.0, self._state.energy - self._cost_per_turn)
```
- [ ] **Step 5: Commit** `feat(memory): B3 energy consumption on cognitive-work turns`

---

### Task 4: 回復（ConsolidationTrigger poll loop, idle-triggered）

**Files:** Modify `consolidation.py`(`__init__` signature、`run()` poll loop 內回復);Test `tests/test_energy.py`

**Interfaces:** `ConsolidationTrigger.__init__` 增 `energy_enabled: bool = False`、`restore_per_tick: float = 0.05`、`energy_idle_threshold_s: int = 600`、`energy_restore_debounce_s: int = 300`。

- [ ] **Step 1: 失敗測試**(用 plan B2 的 `_mk_trigger` 風格 + 注入 energy 參數;直接測一個可單測的回復方法 `_maybe_restore_energy(now)`)

```python
def test_energy_restored_when_user_idle(tmp_path):
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1,
                    energy_idle_threshold_s=600, energy_restore_debounce_s=300)
    t._maybe_restore_energy(now=10_000.0)  # user idle huge, debounce passed
    assert s.energy == 0.3 and s.last_energy_restore_at == 10_000.0

def test_energy_not_restored_when_user_active(tmp_path):
    s = MindState(); s.energy = 0.2; s.last_user_at = 9_900.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)  # only 100s idle < 600
    assert s.energy == 0.2

def test_energy_restore_decoupled_from_consolidation_target(tmp_path):
    # even with no sealed transcript to consolidate (target=None), idle restores energy
    s = MindState(); s.energy = 0.2; s.last_user_at = 0.0; s.last_energy_restore_at = 0.0
    t = _mk_trigger(tmp_path, s, energy_enabled=True, restore_per_tick=0.1)
    t._maybe_restore_energy(now=10_000.0)
    assert s.energy == 0.3  # restored regardless of any consolidation target
```

- [ ] **Step 2-4:** `__init__` 存四個 energy 參數。加方法:
```python
    def _maybe_restore_energy(self, now: float) -> None:
        if not self._energy_enabled:
            return
        if now - self._state.last_user_at < self._energy_idle_threshold_s:
            return
        if now - self._state.last_energy_restore_at < self._energy_restore_debounce_s:
            return
        self._state.energy = min(1.0, self._state.energy + self._restore_per_tick)
        self._state.last_energy_restore_at = now
        save_state(self._state, self._persist_path)
```
在 `run()` poll 迴圈內(每 tick、`_should_consolidate` 判斷**之外/之前**,確保脫鉤)呼叫 `self._maybe_restore_energy(now)`。
- [ ] **Step 5: Commit** `feat(memory): B3 idle-triggered energy restore (decoupled from consolidation)`

---

### Task 5: 注入 [Mind state] + kernel 接線

**Files:** Modify `mind_prompt.py`(`render_mind` 增 `energy_line`、`_render_mindstate` 注入)、`kernel.py`(三處注入);Test `tests/test_energy.py`

**Interfaces:** `render_mind(..., energy_line: str | None = None)`;helper `energy_bucket_line(energy: float) -> str`。

- [ ] **Step 1: 失敗測試**

```python
def test_energy_bucket_line_no_emotion_words():
    from dollos.mind.mind_prompt import energy_bucket_line
    lo = energy_bucket_line(0.3)
    assert "0.3" in lo and "偏低" in lo
    assert "累" not in lo  # no feeling words (autonomy)
    assert "飽滿" in energy_bucket_line(0.9)
    assert "普通" in energy_bucket_line(0.5)

def test_render_mind_omits_energy_when_line_none():
    # render_mind(..., energy_line=None) → output has no 精力 line
def test_render_mind_includes_energy_line():
    # render_mind(..., energy_line="精力: 偏低 (0.3)") → present near mood
```

- [ ] **Step 2-4:** `energy_bucket_line(energy)`:`≥0.7`→`f"精力: 飽滿 ({energy:.1f})"`、`0.4–0.7`→`普通`、`<0.4`→`偏低`(無「累」)。`render_mind` 收 `energy_line`,傳進 `_render_mindstate`,在 mood 行旁輸出(None 則略)。kernel:(a) `MindLoop(energy_enabled=settings.energy.enabled, cost_per_turn=settings.energy.cost_per_turn, ...)`;(b) `ConsolidationTrigger(energy_enabled=..., restore_per_tick=..., energy_idle_threshold_s=..., energy_restore_debounce_s=..., ...)`;(c) render_mind 呼叫處算 `energy_line = energy_bucket_line(state.energy) if settings.energy.enabled else None` 傳入。
- [ ] **Step 5:** 跑 `uv run pytest tests/test_energy.py tests/test_mind_state.py tests/test_config.py -v` + 回歸 `uv run pytest -q | tail -3`(全綠)。Commit `feat(memory): B3 energy injection in [Mind state] + kernel wiring`

---

## Smoke Gate（spec §6,實作後必跑,決定 merge or HELD）

實作完成、全綠後,跑行為驗證(不是單元測,是出場 gate):
1. 寫一個 probe 腳本(仿先前 self-profile probe):用真實 LLM(`http://127.0.0.1:8001`)+ 真 character pack 渲染,固定 `energy=0.9` vs `energy=0.2` 各跑同一 user 情境(例如「跟我聊聊今天」),各 3 次。
2. 啟發式比較:低 energy 的回應是否較短 / 較不主動 / 語氣較淡。
3. **判定**:
   - 有可觀察差異 → B3 通過,正常 final review + merge。
   - 無差異(dead state)→ **B3 HELD**:保留 code(state/接線),roadmap 標「需更強掛點(energy→LLM 參數 / energy→Mood nudge)才有意義」,**誠實記錄不假裝補完**,轉 A1。

## Self-Review

- spec §3.1 state+clamp → Task 1 ✓;§3.5 config → Task 2 ✓;§3.2 消耗(認知工作)→ Task 3 ✓;§3.3 回復(idle/脫鉤)→ Task 4 ✓;§3.4 注入+§3.5 接線 → Task 5 ✓;§6 smoke → Smoke Gate ✓。
- type 一致:`energy_enabled`/`cost_per_turn`(Task 3 MindLoop)、`restore_per_tick`/`energy_idle_threshold_s`/`energy_restore_debounce_s`(Task 4 trigger)、`energy_line`/`energy_bucket_line`(Task 5)跨 task + kernel 注入一致。
- 無 placeholder;測試碼完整;`_turn_had_tool` 在 Task 3 定義 + dispatch 點 set。
