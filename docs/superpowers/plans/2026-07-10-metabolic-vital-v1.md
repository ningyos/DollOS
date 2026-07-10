# 代謝 Vital 模型 v1a+v1b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `state.energy` 的消耗從齊頭 0.05 換成**本回合自己的 token 數**(effort flow)乘上**GPU 熱乘數**,並每回合落一行 code-captured vitals JSONL 當 RL 底材。

**Architecture:** LLM transport 已解析 token 但不回傳;沿既有的 per-call `purpose` threading 加一個 per-turn `on_usage` callback,讓 `MindLoop` 跨 cascade pass 累加自己的 token(污染安全,並行 Workflow/consolidation 永不洩入)。drain site(mind_loop:830)改用 token 公式;熱從 `system_pulse.latest_sample()` 取乘數。RL 訊號用**獨立 `VitalsRecorder`** 在 drain site 發(drain 在 TurnLatencyRecord 發出之後,不能共用)。

**Tech Stack:** Python 3.13, asyncio, pydantic, pytest, uv, httpx。

## Global Constraints

- `cd /home/progcat/Projects/DollOS`;測試 `uv run pytest`。分支 `metabolic-vital-model`(spec 已 commit)。全程留此分支。
- **單軸**:`state.energy` 欄位/型別/持久化**不變**,只換驅動它的數字。
- **drain 公式**(exact):`token_cost = (completion + 0.25·prompt) / token_per_energy_unit`;`drain = token_cost × thermal_multiplier`。
- **D1=a**:token 缺失(prompt/completion 皆 None)→ `token_cost = cost_per_turn`(今天的 0.05),`cost_mode="flat_legacy"`;有 token → `cost_mode="measured"`。這是**唯一被授權的 no-fallback 例外**,必須打 `cost_mode` tag。
- **污染安全**:token 歸因**只**用 per-turn `on_usage` in-loop 累加,**絕不**用 `call_purpose`(到處 "cascade",已驗證壞)。
- **改數字不改敘述**:`energy_bucket_line` 措辭/格式/分桶**不動**。不加新 prompt block、不加疲憊句。
- **不動**:`produced and consumes` gate 與 `external_public` 豁免(mind_loop:827-828)逐字保留;`MoodTool`;`evaluate_alerts`/pulse observer;consolidation 回充(電池=v2,不在本 plan)。
- **no-fallback(D1 以外)**:GPU 熱/瓦缺失 → 乘數 1.0 / 欄位 None,絕不造假。`latest_sample()` 內建 staleness guard(>2× poll → None)。
- **thermal_multiplier 只在 v1b(Task 5)接上**;v1a(Task 2)的 drain 乘數恆 1.0。
- 每 task commit,尾端附:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq
  ```
- Spec:`docs/superpowers/specs/2026-07-10-metabolic-vital-model-design.md`。本 plan = **v1a(Task 1-3)+ v1b(Task 4-5)**;v2 電池天花板不在此(留 LattePanda)。

---

### Task 1: `on_usage` callback 穿透(transport → composed → adapter interface)

**Files:**
- Modify: `src/dollos/llm/adapter.py`(`LLMAdapter.stream_completion` + `stream_messages` 抽象簽名)
- Modify: `src/dollos/llm/composed.py`(`ComposedLLMAdapter` 兩個方法 forward)
- Modify: `src/dollos/llm/transport.py`(`Provider.stream` 簽名 + `finally` 呼叫)
- Test: `tests/` 既有 transport 測試檔(`grep -rln "def.*stream\|Provider\|tokens_predicted" tests/`;無則新建 `tests/test_transport_on_usage.py`)

**Interfaces:**
- Produces: 三層 stream 方法新增 `on_usage: Callable[[int | None, int | None], None] | None = None` 參數;transport 在 `finally` 解析完 token 後 `on_usage(prompt_tokens, completion_tokens)`(guarded)。

- [ ] **Step 1: Write the failing test**

先讀既有 transport 測試怎麼 mock httpx SSE(`grep -rn "aiter_lines\|MockTransport\|respx\|tokens_predicted" tests/`)。若有既有 harness,仿它;斷言:一次含 final SSE(`stop=true` 帶 `tokens_evaluated`/`tokens_predicted`)的 stream 跑完後,傳入的 `on_usage` 被以 `(prompt_tokens, completion_tokens)` 呼叫一次。核心斷言(依實際 harness 調整取用):

```python
async def test_provider_stream_calls_on_usage_with_tokens():
    got = []
    provider = _make_provider_with_fake_sse(  # mirror existing transport test setup
        final_payload={"stop": True, "tokens_evaluated": 128, "tokens_predicted": 42},
    )
    async for _ in provider.stream(prompt="hi", on_usage=lambda p, c: got.append((p, c))):
        pass
    assert got == [(128, 42)]


async def test_provider_stream_on_usage_none_when_no_final_payload():
    got = []
    provider = _make_provider_with_fake_sse(final_payload=None)  # server omitted usage
    async for _ in provider.stream(prompt="hi", on_usage=lambda p, c: got.append((p, c))):
        pass
    assert got == [(None, None)]
```

> 若既有 transport 測試沒有可重用的 fake-SSE harness,建立最小的(mirror `src/dollos/llm/transport.py` 讀 `resp.aiter_lines()` 的形狀)。不要發明與實際 transport 不符的介面。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transport_on_usage.py -v`(或實際檔名)
Expected: FAIL(`stream() got an unexpected keyword argument 'on_usage'`)

- [ ] **Step 3: Write minimal implementation**

(a) `src/dollos/llm/adapter.py` — 兩個抽象方法簽名各加參數(在 `purpose` 後),並在檔頂 import `from collections.abc import AsyncIterator, Callable`(既有已 import AsyncIterator,補 Callable):
```python
        purpose: str = "cascade",
        on_usage: Callable[[int | None, int | None], None] | None = None,
```
(兩個方法都加。docstring 補一句:`on_usage` — invoked once per call in the transport `finally` with (prompt_tokens, completion_tokens); either may be None when the backend omits usage.)

(b) `src/dollos/llm/composed.py` — `ComposedLLMAdapter` 兩個方法同樣加參數,並 forward 給 `self._provider.stream(...)`(加 `on_usage=on_usage,`)。檔頂 import `from collections.abc import Callable`(若無)。

(c) `src/dollos/llm/transport.py` — `Provider.stream` 簽名加 `on_usage=None`(在 `purpose` 後);檔頂 import `Callable`。在 `finally`(line 146-175)解析完 `prompt_tokens`/`completion_tokens` 之後、record 之前,加:
```python
                if on_usage is not None:
                    try:
                        on_usage(prompt_tokens, completion_tokens)
                    except Exception:
                        logger.exception("on_usage callback raised (continuing)")
```
> 放在 `if self._recorder is not None:` 區塊**之前或之後皆可**,但必須在 token 解析(line 148-156)**之後**。guarded like the recorder(callback 拋出不得斷 turn)。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transport_on_usage.py -v`
Then: `uv run pytest tests/ -k "transport or composed or adapter" -q`(既有 stream 測試零回歸 —— 新參數有 default,呼叫端不受影響)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/llm/adapter.py src/dollos/llm/composed.py src/dollos/llm/transport.py tests/test_transport_on_usage.py
git commit -m "feat(vital): thread per-call on_usage callback through LLM stream stack

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 2: MindLoop token 累加 + token 驅動 drain(v1a 行為核心)

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`__init__` 加累加器 + `_token_per_energy_unit`;`_on_turn_usage` 方法;per-turn reset;兩個 adapter 呼叫點加 `on_usage=`;drain site 827-830 換公式)
- Modify: `src/dollos/config.py`(`EnergyConfig` 加 `token_per_energy_unit`)
- Modify: `src/dollos/kernel.py`(把 `settings.energy.token_per_energy_unit` 傳進 `MindLoop`)
- Test: `tests/test_mind_loop_*energy*.py`(既有能量測試檔;`grep -rln "energy" tests/ | grep mind` 找)+ 可能新建

**Interfaces:**
- Consumes: `on_usage`(Task 1)。
- Produces: `MindLoop._on_turn_usage(prompt: int|None, completion: int|None)`;每回合 drain 用 `(completion + 0.25·prompt)/token_per_energy_unit`(measured)或 `cost_per_turn`(flat_legacy);stash `self._turn_energy_cost: float`、`self._turn_cost_mode: str`、`self._turn_tokens_total: int|None`(給 Task 3 的 VitalsRecord 讀)。

- [ ] **Step 1: Write the failing test**

先讀既有能量消耗測試(`grep -rn "energy" tests/test_mind_loop*.py | grep -i "cost\|0.05\|drain\|consume"`)看它們怎麼建 `MindLoop` + 驅動一回合 + 斷言 `state.energy`。仿它加:

```python
# heavy vs light: more tokens → more drain
async def test_token_driven_drain_scales_with_tokens(...):
    ml = make_mindloop(..., token_per_energy_unit=2000.0)   # calibrate helper
    ml._state.energy = 1.0
    # simulate this turn reporting usage via the on_usage path
    ml._reset_turn_tokens_for_test()   # or drive a real turn whose fake provider calls on_usage
    ml._on_turn_usage(400, 1000)       # prompt=400, completion=1000
    # then drive the drain (or call the drain helper) with produced=True, internal origin
    ...
    # cost = (1000 + 0.25*400)/2000 = 1100/2000 = 0.55
    assert abs((1.0 - ml._state.energy) - 0.55) < 1e-6
    assert ml._turn_cost_mode == "measured"

async def test_missing_tokens_uses_flat_legacy(...):
    ml = make_mindloop(..., token_per_energy_unit=2000.0, cost_per_turn=0.05)
    ml._state.energy = 1.0
    # no on_usage call OR on_usage(None, None) → accumulators stay None
    ...
    assert abs((1.0 - ml._state.energy) - 0.05) < 1e-6
    assert ml._turn_cost_mode == "flat_legacy"
```

> 對齊既有 `make_mindloop` fixture(勿發明新形狀)。若既有能量測試是「驅動真回合」式的,優先**驅動真回合**+假 provider 在 stream 呼叫 `on_usage`(端到端更強);若太重則直接測 drain 邏輯(如上,呼叫 `_on_turn_usage` 後觸發 drain 路徑)。實作者擇既有測試風格對齊的一種。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mind_loop_metabolic.py -v`(或你放的檔)
Expected: FAIL(`_on_turn_usage` / `_turn_cost_mode` 不存在,或 drain 仍是 0.05)

- [ ] **Step 3: Write minimal implementation**

(a) `config.py` `EnergyConfig` 加:
```python
    token_per_energy_unit: float = 2000.0   # calibrated so a typical turn ≈ cost_per_turn; smoke-tuned (spec §7 D4)
```

(b) `kernel.py` — 建 `MindLoop(...)` 處把 `token_per_energy_unit=settings.energy.token_per_energy_unit` 傳入(對齊既有 `cost_per_turn=settings.energy.cost_per_turn` 的傳法;先 grep `cost_per_turn=` in kernel.py)。

(c) `mind_loop.py`:
- `__init__` 簽名加 `token_per_energy_unit: float = 2000.0`;存 `self._token_per_energy_unit = token_per_energy_unit`。在 `_turn_speech` 累加器附近加:
```python
        # 代謝 vital (spec 2026-07-10 §2.2): this turn's own token effort,
        # accumulated across cascade passes via on_usage. Contamination-proof:
        # only THIS loop's calls hit THIS loop's callback. Cleared per turn.
        self._turn_prompt_tokens: int | None = None
        self._turn_completion_tokens: int | None = None
        # stashed at the drain site for Task 3's VitalsRecord.
        self._turn_energy_cost: float = 0.0
        self._turn_cost_mode: str = "measured"
        self._turn_tokens_total: int | None = None
```
- 加方法:
```python
    def _on_turn_usage(self, prompt: int | None, completion: int | None) -> None:
        """Accumulate this turn's own token usage (one call per cascade pass).
        A None-usage pass contributes nothing; the turn total stays None only
        when NO pass reported usage (→ flat_legacy at the drain site)."""
        if prompt is not None:
            self._turn_prompt_tokens = (self._turn_prompt_tokens or 0) + prompt
        if completion is not None:
            self._turn_completion_tokens = (self._turn_completion_tokens or 0) + completion
```
- **per-turn reset**:在 `_run_one_turn` 裡 `_turn_speech` 被清空的同一處(`grep -n "_turn_speech = \[\]\|_turn_speech.clear" src/dollos/mind/mind_loop.py`,約在呼叫 `_llm_iterate` 之前)加:
```python
        self._turn_prompt_tokens = None
        self._turn_completion_tokens = None
```
- **兩個 adapter 呼叫點**(line 1507 `stream_completion`、1516 `stream_messages`)各加 `on_usage=self._on_turn_usage,`。
- **drain site**(827-830)—— gate 逐字保留,只換扣血:
```python
        produced = bool(self._turn_speech) or self._turn_had_tool
        consumes = self._ctx.origin_tier != "external_public"
        if self._energy_enabled and produced and consumes:
            p = self._turn_prompt_tokens
            c = self._turn_completion_tokens
            if p is not None or c is not None:                    # measured
                token_cost = ((c or 0) + 0.25 * (p or 0)) / self._token_per_energy_unit
                cost_mode = "measured"
                tokens_total = (c or 0) + (p or 0)
            else:                                                  # D1=a sanctioned degrade
                token_cost = self._cost_per_turn
                cost_mode = "flat_legacy"
                tokens_total = None
            # v1a: thermal multiplier = 1.0 (Task 5 wires the real one here)
            drain = token_cost
            self._state.energy = max(0.0, self._state.energy - drain)
            self._turn_energy_cost = drain
            self._turn_cost_mode = cost_mode
            self._turn_tokens_total = tokens_total
```
> `_turn_had_tool` 是既有欄位(gate 已在用);若實際名稱不同,以既有 gate 那行為準。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mind_loop_metabolic.py -v`
Then: `uv run pytest -q`(全套;既有齊頭-0.05 能量測試若斷言舊值需更新為 token 公式或改用 flat_legacy 路徑 —— 更新它們,別刪測試意圖)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py src/dollos/config.py src/dollos/kernel.py tests/
git commit -m "feat(vital): token-driven energy drain (measured/flat_legacy), per-turn attribution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 3: `VitalsRecorder` + drain site 發 per-turn RL 訊號

drain 在 `_run_one_turn`:830(`_llm_iterate` 的 `TurnLatencyRecord` 已於更早發出)→ 用**獨立** vitals 記錄,不塞 TurnLatencyRecord。

**Files:**
- Create: `src/dollos/telemetry/vitals.py`(`VitalsRecord` + `VitalsRecorder`,mirror `telemetry/turn_latency.py`)
- Modify: `src/dollos/mind/mind_loop.py`(drain site 後發 record;`__init__` 收 `vitals_recorder=None`)
- Modify: `src/dollos/kernel.py`(建 `VitalsRecorder` 傳入 MindLoop,mirror `turn_latency_recorder`)
- Test: `tests/test_vitals_recorder.py`(新建)

**Interfaces:**
- Consumes: `self._turn_energy_cost`/`_turn_cost_mode`/`_turn_tokens_total`(Task 2)。
- Produces: `VitalsRecord(ts, turn_id, tokens_total, energy_cost, energy_after, cost_mode)`(v1b 再加 ambient);`VitalsRecorder.record()` append daily JSONL,mirror `TurnLatencyRecorder`。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_vitals_recorder.py`(mirror `tests/` 既有 turn_latency recorder 測試風格):

```python
import json
from dollos.telemetry.vitals import VitalsRecord, VitalsRecorder


async def test_vitals_recorder_writes_row(tmp_path):
    rec = VitalsRecorder(tmp_path)
    await rec.record(VitalsRecord(
        ts=1_000_000.0, turn_id="t-abc", tokens_total=1100,
        energy_cost=0.55, energy_after=0.45, cost_mode="measured",
    ))
    files = list(tmp_path.glob("vitals-*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text().strip())
    assert row["turn_id"] == "t-abc" and row["energy_cost"] == 0.55
    assert row["cost_mode"] == "measured" and row["tokens_total"] == 1100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_vitals_recorder.py -v`
Expected: FAIL(`No module named 'dollos.telemetry.vitals'`)

- [ ] **Step 3: Write minimal implementation**

(a) `src/dollos/telemetry/vitals.py`(mirror `turn_latency.py` 的 dataclass + Recorder + asyncio.Lock + daily file + to_json + try/except-log-not-raise):
```python
"""VitalsRecorder — per-turn metabolic vitals (append-only daily JSONL).

One row per turn at the energy-drain site = the (state, action, cost, state')
tuple for future RL. Code-captured only (model can never self-report effort —
spec §5 provenance rule). No fake fallback: absent ambient fields stay None;
write failure logs, never raises.
"""
from __future__ import annotations
import asyncio, json, logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
logger = logging.getLogger(__name__)


@dataclass
class VitalsRecord:
    ts: float
    turn_id: str | None
    tokens_total: int | None
    energy_cost: float
    energy_after: float
    cost_mode: str            # "measured" | "flat_legacy"
    # v1b ambient (Task 5); None until then
    gpu_hottest_c: float | None = None
    gpu_power_w: float | None = None
    battery_pct: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class VitalsRecorder:
    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._lock = asyncio.Lock()

    async def record(self, rec: VitalsRecord) -> None:
        async with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(rec.ts).date()
                path = self._dir / f"vitals-{day:%Y-%m-%d}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(rec.to_json()); f.write("\n")
            except Exception:
                logger.exception("vitals record failed (continuing)")
```

(b) `mind_loop.py` — `__init__` 加 `vitals_recorder=None` → `self._vitals_recorder = vitals_recorder`。drain site(Task 2 的 block)在扣血 stash 之後加(需 turn_id:`grep -n "turn_id" src/dollos/mind/mind_loop.py` 找它 stash 在哪 —— TurnLatencyRecord 已用,readable at 830):
```python
            if self._vitals_recorder is not None:
                import time as _time
                try:
                    await self._vitals_recorder.record(VitalsRecord(
                        ts=_time.time(),
                        turn_id=self._current_turn_id,   # same stash TurnLatencyRecord uses
                        tokens_total=self._turn_tokens_total,
                        energy_cost=self._turn_energy_cost,
                        energy_after=self._state.energy,
                        cost_mode=self._turn_cost_mode,
                    ))
                except Exception:
                    logger.exception("vitals record dispatch failed; continuing")
```
> import `VitalsRecord` 於檔頂(`from dollos.telemetry.vitals import VitalsRecord`)。`self._current_turn_id` 用 TurnLatencyRecord 讀的同一個 stash(確認實際名稱;line 1240 附近註解說 turn_id always readable)。`_time.time()` 用既有 import 慣例(檔頂已 `import time` 則直接 `time.time()`)。

(c) `kernel.py` — 建 `VitalsRecorder(settings.data.root / "traces" / "vitals")`(mirror `turn_latency_recorder` 的目錄慣例;grep `TurnLatencyRecorder(` in kernel.py 對齊),傳 `vitals_recorder=` 進 MindLoop。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_vitals_recorder.py -v` then `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/telemetry/vitals.py src/dollos/mind/mind_loop.py src/dollos/kernel.py tests/test_vitals_recorder.py
git commit -m "feat(vital): VitalsRecorder — per-turn RL substrate at the drain site

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 4: nvidia-smi `power.draw` + 平行 `gpu_power` 欄位(v1b 量測)

**Files:**
- Modify: `src/dollos/perception/system_pulse.py`(`read_nvidia_smi` 查詢字串 + 解析;`PulseSample` 加 `gpu_power`;`_poll_once` 填;`render_block` 可選瓦顯示)
- Test: `tests/test_system_pulse.py`(追加)

**Interfaces:**
- Produces: `PulseSample.gpu_power: list[tuple[float, float]] | None`(每 GPU (draw_w, limit_w),與 `gpus` 1:1;None 當來源缺失)。

- [ ] **Step 1: Write the failing test**

`read_nvidia_smi` 現在跑真 subprocess;測試改測**解析**部分(把 nvidia-smi 輸出解析抽成純函式,或用 monkeypatch `_run_cmd` 回假 CSV)。先讀 `read_nvidia_smi`(system_pulse.py:168-193)看它怎麼被測(既有 test 可能 monkeypatch `_run_cmd`)。加:

```python
async def test_nvidia_parses_power_draw_parallel(monkeypatch):
    # fake nvidia-smi CSV: memory.used, memory.total, temperature.gpu, power.draw, power.limit
    fake = "1024, 8192, 70, 120.5, 165.0\n"
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(fake))
    gpus, gpu_power = await read_nvidia_smi()   # NOTE new return shape — see impl note
    assert gpus == [(1024/8192*100.0, 70.0)]
    assert gpu_power == [(120.5, 165.0)]


async def test_nvidia_power_absent_columns_still_parses_mem_temp(monkeypatch):
    fake = "1024, 8192, 70\n"   # old 3-col output (driver without power telemetry)
    monkeypatch.setattr("dollos.perception.system_pulse._run_cmd", _const_async(fake))
    gpus, gpu_power = await read_nvidia_smi()
    assert gpus == [(1024/8192*100.0, 70.0)]
    assert gpu_power == [] or gpu_power is None   # defensive: power omitted, mem/temp intact
```

> `read_nvidia_smi` 目前回 `list[tuple]`;本 task 改回 `tuple[list, list]`(gpus, gpu_power)。**更新既有的 read_nvidia_smi 呼叫點**(`_poll_once`)。`_const_async` = 回傳固定字串的假 async(仿既有測試 helper;無則自建)。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_system_pulse.py -k nvidia -v`
Expected: FAIL(回傳形狀不符 / 無 power 解析)

- [ ] **Step 3: Write minimal implementation**

`system_pulse.py`:
- 查詢字串加欄位:`"--query-gpu=memory.used,memory.total,temperature.gpu,power.draw,power.limit"`。
- `read_nvidia_smi` 改回 `tuple[list[tuple[float,float]], list[tuple[float,float]]]`(gpus, gpu_power)。**防禦性解析**:mem/temp(前 3 欄)照舊;power.draw/limit(第 4-5 欄)獨立 try/except → 該行 power 缺失時**只**跳過 power(append 不進 gpu_power),**不丟**該行的 mem/temp:
```python
    gpus, gpu_power = [], []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3: continue
        try:
            used, total, temp = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError: continue
        if total <= 0: continue
        gpus.append(((used/total)*100.0, temp))
        draw = lim = None
        if len(parts) >= 5:
            try: draw, lim = float(parts[3]), float(parts[4])
            except ValueError: draw = lim = None
        if draw is not None and lim is not None:
            gpu_power.append((draw, lim))
    return gpus, gpu_power
```
- `PulseSample` 加 `gpu_power: list[tuple[float, float]] = field(default_factory=list)`(additive;**不動** `gpus`/`signature()`)。
- `_poll_once`:`gpus, gpu_power = await read_nvidia_smi()`(現在 `read_nvidia_smi()` 在 `asyncio.gather` 裡 —— 拆出來或 adapt;先看 `_poll_once` 怎麼呼叫)。填進 `PulseSample(..., gpu_power=gpu_power)`。
- `render_block`(可選瓦顯示,presence-gated):在既有 `vital heat` 行,若 `sample.gpu_power` 非空,綴 ` · {draw:.0f}w`(取最熱 GPU 對應那顆的 draw,或 sum;簡單取 max draw)。None/空 → 不綴。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_system_pulse.py -v` then `uv run pytest -q`
Expected: PASS(既有 pulse 測試若斷言 `read_nvidia_smi` 舊回傳形狀需更新)

- [ ] **Step 5: Commit**

```bash
git add src/dollos/perception/system_pulse.py tests/test_system_pulse.py
git commit -m "feat(vital): query GPU power.draw into a parallel gpu_power field (log-only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

### Task 5: `thermal_multiplier` 接進 drain + ambient 進 VitalsRecord(v1b 耦合)

**Files:**
- Modify: `src/dollos/perception/system_pulse.py`(加純 helper `thermal_multiplier`)
- Modify: `src/dollos/mind/mind_loop.py`(drain 乘上 thermal_multiplier;VitalsRecord 填 ambient)
- Modify: `src/dollos/config.py`(`EnergyConfig` 加 `thermal_multiplier_warm/hot`)
- Modify: `src/dollos/kernel.py`(傳新 config)
- Test: `tests/test_system_pulse.py`(thermal_multiplier)+ `tests/test_mind_loop_metabolic.py`(drain×熱)

**Interfaces:**
- Consumes: `PulseSample.gpus`(temp)、`gpu_power`(Task 4);Task 2 的 drain block;Task 3 的 VitalsRecord。
- Produces: `thermal_multiplier(temp_c: float, warm: float, hot: float) -> float`(cool→1.0/warm→warm/hot→hot,用既有 `bucket_gpu_temp`)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_system_pulse.py
from dollos.perception.system_pulse import thermal_multiplier

def test_thermal_multiplier_buckets():
    assert thermal_multiplier(40.0, 1.15, 1.4) == 1.0    # cool (<55)
    assert thermal_multiplier(65.0, 1.15, 1.4) == 1.15   # warm (55-75)
    assert thermal_multiplier(90.0, 1.15, 1.4) == 1.4    # hot (>75)
```
```python
# tests/test_mind_loop_metabolic.py — hot GPU raises the drain
async def test_hot_gpu_raises_drain(...):
    ml = make_mindloop(..., token_per_energy_unit=2000.0,
                       thermal_multiplier_warm=1.15, thermal_multiplier_hot=1.4)
    ml._system_pulse = _fake_pulse(latest=_sample(gpus=[(50.0, 90.0)]))  # hot
    ml._state.energy = 1.0
    ml._on_turn_usage(0, 1000)   # token_cost = 1000/2000 = 0.5
    <drive drain>
    # drain = 0.5 * 1.4 = 0.7
    assert abs((1.0 - ml._state.energy) - 0.7) < 1e-6

async def test_stale_or_absent_sample_multiplier_one(...):
    ml._system_pulse = _fake_pulse(latest=None)   # stale/absent → 1.0
    ...
    assert abs((1.0 - ml._state.energy) - 0.5) < 1e-6   # no multiplier
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_system_pulse.py -k thermal tests/test_mind_loop_metabolic.py -k gpu -v`
Expected: FAIL(`thermal_multiplier` 未定義 / drain 未乘熱)

- [ ] **Step 3: Write minimal implementation**

(a) `system_pulse.py` — 與 `bucket_gpu_temp` 同置:
```python
def thermal_multiplier(temp_c: float, warm: float, hot: float) -> float:
    b = bucket_gpu_temp(temp_c)
    return hot if b == "hot" else warm if b == "warm" else 1.0
```

(b) `config.py` `EnergyConfig` 加:
```python
    thermal_multiplier_warm: float = 1.15
    thermal_multiplier_hot: float = 1.4
```

(c) `kernel.py` — 傳 `thermal_multiplier_warm=`/`thermal_multiplier_hot=` 進 MindLoop(對齊 token_per_energy_unit 的傳法)。

(d) `mind_loop.py`:
- `__init__` 收 `thermal_multiplier_warm=1.15`, `thermal_multiplier_hot=1.4` → 存。
- drain block(Task 2)把 `drain = token_cost` 換成套熱乘數 + 記 ambient:
```python
            mult = 1.0
            hottest_c = gpu_w = batt = None
            if self._system_pulse is not None:
                try:
                    s = self._system_pulse.latest_sample()
                except Exception:
                    s = None
                if s is not None:
                    if s.gpus:
                        hottest_c = max(t for _, t in s.gpus)
                        mult = thermal_multiplier(hottest_c, self._thermal_mult_warm, self._thermal_mult_hot)
                    if getattr(s, "gpu_power", None):
                        gpu_w = max(d for d, _ in s.gpu_power)
                    batt = s.battery_pct
            drain = token_cost * mult
            self._state.energy = max(0.0, self._state.energy - drain)
            self._turn_energy_cost = drain
            self._turn_cost_mode = cost_mode
            self._turn_tokens_total = tokens_total
            self._turn_ambient = (hottest_c, gpu_w, batt)   # for VitalsRecord
```
- Task 3 的 VitalsRecord 呼叫填 ambient:`gpu_hottest_c=self._turn_ambient[0], gpu_power_w=self._turn_ambient[1], battery_pct=self._turn_ambient[2]`(初始化 `self._turn_ambient = (None, None, None)` 於 `__init__`)。import `thermal_multiplier`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_system_pulse.py tests/test_mind_loop_metabolic.py -v` then `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/dollos/perception/system_pulse.py src/dollos/mind/mind_loop.py src/dollos/config.py src/dollos/kernel.py tests/
git commit -m "feat(vital): thermal multiplier on drain + ambient heat/watts/battery in vitals log

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PSi9Gwhou1PAg2ZdczRMGq"
```

---

## Live Smoke(人工,實作後、merge 前)

真 LLM@8001 + 真 kernel harness(承前兩 feature 的 smoke)+ scratch data.root:
1. **effort 分級**:一句話回合 vs 觸發 `SpawnWorkflow`/長 Recall 的重回合 → dump `vitals-*.jsonl`,確認重回合 `energy_cost` 明顯 > 輕回合、`cost_mode=="measured"`、`tokens_total` 合理。
2. **flat_legacy**:模擬 provider 不回 usage(或臨時擋 token 解析)→ 該回合 `energy_cost≈0.05`、`cost_mode=="flat_legacy"`。
3. **熱乘數**:GPU 負載到 hot bucket(或注入 hot 假 sample)→ 同 token 量 `energy_cost` 變高;`gpu_hottest_c`/`gpu_power_w` 進 log。
4. **不改敘述**:dump 一回合 prompt,確認 `energy_bucket_line` 格式/措辭不變、無新疲憊句。
5. **湧現觀察**:連續重度工具使用把 energy 壓到 <0.5 → 確認 `_AGENDA_ENERGY_FLOOR` 讓自主議程回合變少(reactive 不受影響);觀察她是否自然變簡短/提及累(不強求,記錄)。
6. **校準**:用 §7-D4,拿一週 `llm_calls-*.jsonl` 反推 `token_per_energy_unit` 使典型回合≈0.05,寫回 config 預設。

---

## Self-Review(plan 對 spec)

**1. Spec coverage:** §2.2 token flow→T2;§2.3 熱乘數→T5;§2.4 瓦數 log-only→T4;§4.2 污染安全歸因(A in-loop)→T1+T2;§4.3 power.draw 平行欄位+防禦解析→T4;§4.4 telemetry(turn_id/vitals)→T3(VitalsRecord 帶 turn_id;LLMCallRecord turn_id 延後至有 offline-join 需要時,記註);§5 RL 底材+code-captured→T3;§3 改數字不改敘述→T2/T5 不動 energy_bucket_line、Global Constraints 明列。v1b 完;**v2 電池、LLMCallRecord.turn_id、Φ(energy) recipe、ConserveMode 明確不在本 plan**。

**2. Placeholder scan:** 校準常數(2000.0/1.15/1.4)標為 smoke-tuned(spec §7-D4 開放項,非佔位)。Task 1/2/4 測試步驟給「對齊既有 harness/fixture」指引(避免發明錯形狀;斷言目標明確)。code step 均完整可貼。

**3. Type consistency:** `on_usage: Callable[[int|None,int|None],None]|None`、`_on_turn_usage(prompt,completion)`、`token_cost=(c+0.25p)/K`、`_turn_energy_cost/_turn_cost_mode/_turn_tokens_total/_turn_ambient`、`VitalsRecord(ts,turn_id,tokens_total,energy_cost,energy_after,cost_mode,+ambient)`、`thermal_multiplier(temp,warm,hot)`、`PulseSample.gpu_power: list[tuple[float,float]]`、`read_nvidia_smi()->tuple[list,list]` 跨 task 一致。

**已知風險(記,非阻塞):** `read_nvidia_smi` 回傳形狀由 `list`→`tuple[list,list]` 是 breaking,T4 必須同步更新 `_poll_once` 唯一呼叫點(spec §8 已列 additive gpu_power 緩解 PulseSample 面,但 read_nvidia_smi 的呼叫點要一起改)。既有齊頭-0.05 能量測試會因 T2 需更新斷言。
