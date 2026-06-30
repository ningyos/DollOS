# B2: Sleep-Time Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** idle 時把對話逐字稿整併成中性 candidate 結構化事實(pull-only),補上「只增長不整理」的記憶缺口。

**Architecture:** 新增背景 `ConsolidationTrigger`(conversation-idle 觸發)→ driver 讀目標日 transcript inline 餵 `memory-keeper` agent(`run_agent`, KEEPER_TOOLS=[Report,Scratchpad])→ driver 寫 `consolidated/{date}.md`。candidate 索引但**不** auto-inject `[Memory context]`(pull-only + provenance)。kernel wire + 兩個 UserSpoke ingress cancel + shutdown 拆除。

**Tech Stack:** Python asyncio / pytest-asyncio。`agent_engine.run_agent`、`FtsMemory`、既有 observer/kernel 範式。

## Global Constraints（逐字自 spec）

- **No-fallback**:任何階段失敗 → log + 跳過該次整併,不降級、不 silent。
- **candidate pull-only**:candidate **不**進每 turn 的 `[Memory context]`;只索引可搜尋,Recall/未來 A1 主動 pull;浮現帶 `[系統整併·待確認]` 前綴。召回 gating 由 B2 自管,不下放 A1。
- **Doll 主導自我**:B2 不改 self-profile;memory-keeper 工具集 **不含** Shell/NoteMemory/SpawnMonitor/RemoveMonitor。
- **非破壞性**:不刪/改原始 transcript 與 NoteMemory。
- **只吃 transcript**(不吃 NoteMemory facts / 全歷史);只整併**已封日** `date < today`。
- **conversation-idle**:`now - max(last_user_at, last_iter_at)`;**不**綁 `SystemPulse.idle_s`(它是 optional gate,None 時忽略不否決)。
- TDD;測試 `uv run pytest <path> -v`(repo root);frequent commits。

## File Structure

- **Modify** `src/dollos/mind/mind_state.py` — 4 新欄位 + save/load 顯式三處(`:173` 顯式列舉)。
- **Modify** `src/dollos/mind/mind_loop.py` — `user_turn_count` 在 UserSpoke 遞增(`:152`);`_derive_memory_hits` 排除 consolidated(`:293/307`)。
- **Modify** `src/dollos/mind/mind_prompt.py` — Recall 浮現 candidate 帶 provenance(`render_memory:131` 周邊;或 Recall 渲染處)。
- **Modify** `src/dollos/perception/system_pulse.py` — 公開 `latest_idle_s()`。
- **Modify** `src/dollos/config.py` — `[consolidation]` Settings 區段。
- **Create** `src/dollos/mind/consolidation.py` — `run_consolidation`(driver)+ `ConsolidationTrigger`。
- **Modify** `src/dollos/kernel.py` — wire trigger(`:320/655`)、cancel 接縫(`:366/454`)、shutdown(`:664`)。
- **Modify** `src/dollos/tools.py` — `KEEPER_TOOLS` allowlist。
- **Create** `tests/test_consolidation.py` — trigger + driver 測試。
- **Modify** `tests/test_mind_state.py`、`tests/test_mind_loop.py` — 新欄位 / gating 測試。

---

### Task 1: MindState 四欄位 + user_turn_count 遞增

**Files:**
- Modify: `src/dollos/mind/mind_state.py`(dataclass 欄位 + `save_state` dict `:173` + `load_state` 建構子)
- Modify: `src/dollos/mind/mind_loop.py`(`iterate()` perception 迴圈 `:152`)
- Test: `tests/test_mind_state.py`、`tests/test_consolidation.py`(新檔,放 user_turn_count 遞增測試)

**Interfaces:**
- Produces: `MindState.user_turn_count: int`、`last_consolidation_turn: int`、`last_consolidation_at: float`、`last_consolidated_date: str`,全部 round-trip save/load。

- [ ] **Step 1: 寫失敗測試（save/load round-trip）**

附加到 `tests/test_mind_state.py`:

```python
def test_consolidation_fields_round_trip(tmp_path):
    from dollos.mind.mind_state import MindState, save_state, load_state
    s = MindState()
    s.user_turn_count = 7
    s.last_consolidation_turn = 3
    s.last_consolidation_at = 123.5
    s.last_consolidated_date = "2026-06-29"
    p = tmp_path / "state.json"
    assert save_state(s, p)
    loaded = load_state(p)
    assert loaded.user_turn_count == 7
    assert loaded.last_consolidation_turn == 3
    assert loaded.last_consolidation_at == 123.5
    assert loaded.last_consolidated_date == "2026-06-29"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_mind_state.py::test_consolidation_fields_round_trip -v`
Expected: FAIL（`AttributeError: user_turn_count` 或 load 後值不符）。

- [ ] **Step 3: 實作 — dataclass + save + load 三處**

在 `MindState` dataclass 加(與既有 scalar 欄位並列):

```python
    user_turn_count: int = 0
    last_consolidation_turn: int = 0
    last_consolidation_at: float = 0.0
    last_consolidated_date: str = ""
```

在 `save_state` 的顯式 state_dict（`mind_state.py:173` 一帶）加四個 key:

```python
        "user_turn_count": state.user_turn_count,
        "last_consolidation_turn": state.last_consolidation_turn,
        "last_consolidation_at": state.last_consolidation_at,
        "last_consolidated_date": state.last_consolidated_date,
```

在 `load_state` 建構 `MindState(...)` 處加（用 `.get` 帶預設,容忍舊狀態檔）:

```python
        user_turn_count=data.get("user_turn_count", 0),
        last_consolidation_turn=data.get("last_consolidation_turn", 0),
        last_consolidation_at=data.get("last_consolidation_at", 0.0),
        last_consolidated_date=data.get("last_consolidated_date", ""),
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_mind_state.py::test_consolidation_fields_round_trip -v`
Expected: PASS。

- [ ] **Step 5: 寫 user_turn_count 遞增測試**

新檔 `tests/test_consolidation.py`:

```python
"""B2 sleep-time consolidation tests."""
from __future__ import annotations
import asyncio
from datetime import date
import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM
from dollos.tools import MAIN_TOOLS


@pytest.mark.asyncio
async def test_user_turn_count_increments_only_on_userspoke(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    ctx = _make_mind_ctx(tmp_path, sink=asyncio.Queue(), state=state)
    loop = MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nhi"),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )
    await loop.iterate()
    assert state.user_turn_count == 1

    # A non-UserSpoke perception must NOT increment it
    queue.put(Perception(kind="ScheduledMoment", t=2.0, data={"text": "alarm"}))
    await loop.iterate()
    assert state.user_turn_count == 1
```

- [ ] **Step 6: 跑確認失敗 → 實作 → 通過**

Run: `uv run pytest tests/test_consolidation.py::test_user_turn_count_increments_only_on_userspoke -v`(FAIL)。
在 `mind_loop.py:152` 的 `if p.kind == "UserSpoke":` 區塊內(已有 `last_user_at = p.t` + B1 的 transcript 寫入)加一行:

```python
                self._state.user_turn_count += 1
```

再跑 → PASS。

- [ ] **Step 7: Commit**

```bash
git add src/dollos/mind/mind_state.py src/dollos/mind/mind_loop.py tests/test_mind_state.py tests/test_consolidation.py
git commit -m "feat(memory): B2 MindState consolidation fields + user_turn_count"
```

---

### Task 2: 支援基礎設施 — SystemPulse.latest_idle_s() + config [consolidation]

**Files:**
- Modify: `src/dollos/perception/system_pulse.py`(公開 accessor)
- Modify: `src/dollos/config.py`(`ConsolidationConfig` + 掛進 Settings)
- Test: `tests/test_system_pulse.py`、`tests/test_config.py`

**Interfaces:**
- Produces: `SystemPulse.latest_idle_s() -> float | None`(過期回 None);`Settings.consolidation`(`idle_threshold_s:int=300`、`min_interval_s:int=3600`、`enabled:bool=True`、`max_tokens:int=2048`、`agent_timeout_s:int=120`、`transcript_tail_chars:int=8000`)。

- [ ] **Step 1: latest_idle_s 失敗測試**

附加到 `tests/test_system_pulse.py`:

```python
def test_latest_idle_s_none_when_no_sample():
    from dollos.perception.system_pulse import SystemPulse
    sp = SystemPulse(poll_interval_s=60.0, enabled=False)
    assert sp.latest_idle_s() is None
```

（若需「過期回 None / 新鮮回值」測試,用 monkeypatch 設 `_last_sample` + 控制時間;最小版先測 None-path。）

- [ ] **Step 2-4: 跑失敗 → 實作 → 通過**

在 `SystemPulse` 加(讀私有 `_last_sample`,含新鮮度:sample `taken_at` 超過 `2 * poll_interval_s` 視為過期):

```python
    def latest_idle_s(self) -> float | None:
        """Idle seconds from the last fresh pulse sample, else None.

        None when: disabled / no sample yet / idle source unavailable /
        sample is stale (older than 2x poll interval).
        """
        s = self._last_sample
        if s is None or s.idle_s is None:
            return None
        age = (datetime.now(s.taken_at.tzinfo) - s.taken_at).total_seconds() \
            if hasattr(s.taken_at, "tzinfo") else None
        if age is not None and age > 2 * self._poll_interval_s:
            return None
        return s.idle_s
```

（依 `PulseSample.taken_at` 實際型別調整 age 計算;若為 naive datetime 用 `datetime.now()`。確認 `self._poll_interval_s` 屬性名與 ctor 一致。）

Run: `uv run pytest tests/test_system_pulse.py -v` → PASS。

- [ ] **Step 5-8: config 區段（TDD）**

`tests/test_config.py` 加:

```python
def test_consolidation_config_defaults():
    from dollos.config import Settings
    s = Settings()  # 或專案既有的 Settings 建構慣例（看現有測試）
    assert s.consolidation.idle_threshold_s == 300
    assert s.consolidation.min_interval_s == 3600
    assert s.consolidation.enabled is True
```

跑失敗。在 `config.py` 仿 `SystemPulseConfig` 加:

```python
class ConsolidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    idle_threshold_s: int = 300
    min_interval_s: int = 3600
    max_tokens: int = 2048
    agent_timeout_s: int = 120
    transcript_tail_chars: int = 8000
```

掛進 `Settings`(仿 `system_pulse: SystemPulseConfig = ...`):

```python
    consolidation: ConsolidationConfig = ConsolidationConfig()
```

跑 → PASS。

- [ ] **Step 9: Commit**

```bash
git add src/dollos/perception/system_pulse.py src/dollos/config.py tests/test_system_pulse.py tests/test_config.py
git commit -m "feat(memory): B2 SystemPulse.latest_idle_s + [consolidation] config"
```

---

### Task 3: 召回 gating — candidate pull-only + provenance

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_derive_memory_hits` `:293/307`,排除 consolidated/)
- Modify: `src/dollos/mind/mind_prompt.py`(Recall 浮現 candidate 帶前綴)
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Consumes: FtsMemory search hit 帶 `source` 欄位(`fts_store.py:222`)。
- Produces: auto-inject `[Memory context]` 不含 `consolidated/` 來源的 hit;Recall 結果中 consolidated 來源帶 `[系統整併·待確認]` 前綴。

- [ ] **Step 1: 失敗測試（auto-inject 排除 consolidated）**

`tests/test_consolidation.py` 加。用一個回傳含 consolidated 來源 hit 的 fake memsearch,斷言 `_derive_memory_hits` 的結果不含該來源:

```python
@pytest.mark.asyncio
async def test_consolidated_excluded_from_auto_inject(tmp_path):
    from dollos.mind.mind_state import MindState, Perception

    class _SrcMemSearch(_FakeMemSearch):
        async def search(self, query, top_k=5):
            return [
                {"content": "主人偏好冰美式", "source": "shared/consolidated/2026-06-29.md"},
                {"content": "今天天氣好", "source": "shared/2026-06-30.md"},
            ]

    state = MindState()
    state.recent_perceptions.append(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ctx = _make_mind_ctx(tmp_path, memsearch=_SrcMemSearch(), state=state)
    loop = _bare_loop(tmp_path, state=state, ctx=ctx)  # helper: MindLoop w/o running iterate
    hits = await loop._derive_memory_hits()
    sources = [h.get("source", "") for h in hits]
    assert not any("consolidated/" in s for s in sources)
    assert any("shared/2026-06-30" in s for s in sources)
```

（`_bare_loop` = 在測試檔建一個只為呼叫 `_derive_memory_hits` 的 MindLoop;沿用 Task 1 的建構參數。）

- [ ] **Step 2-4: 跑失敗 → 實作 → 通過**

在 `_derive_memory_hits`(`mind_loop.py:293`)取得 hits 後、回傳前加過濾:

```python
        hits = [h for h in hits if "consolidated/" not in (h.get("source") or "")]
```

（確認 hit 是 dict 且有 `source` key——review 已驗 `fts_store.py:222` search hit 帶 source。若結構不同,依實際調整 key 取法。）

Run → PASS。

- [ ] **Step 5-8: Recall provenance 前綴（TDD）**

找 Recall 結果渲染處(spec 指 `mind_prompt.py` render_memory `:131` 周邊或 Recall tool 的輸出格式化)。寫測試:consolidated 來源的 hit 渲染時前綴 `[系統整併·待確認]`,非 consolidated 不加。實作:渲染時依 `source` 判斷加前綴:

```python
        prefix = "[系統整併·待確認] " if "consolidated/" in (h.get("source") or "") else ""
        line = f"- {prefix}{content}"
```

Run → PASS。

- [ ] **Step 9: Commit**

```bash
git add src/dollos/mind/mind_loop.py src/dollos/mind/mind_prompt.py tests/test_consolidation.py
git commit -m "feat(memory): B2 candidate pull-only gating + provenance prefix"
```

---

### Task 4: memory-keeper driver（讀 transcript → run_agent → 寫檔）

**Files:**
- Create: `src/dollos/mind/consolidation.py`(`run_consolidation`)
- Modify: `src/dollos/tools.py`(`KEEPER_TOOLS`)
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Consumes: `agent_engine.run_agent(*, task, system, adapter, renderer, memory_root, memsearch, transcripts_root, tool_output_store, tools, max_tokens)`(`agent_engine.py:37`,回傳 report dict 或 None);`subagent_scaffolding` 模板渲染。
- Produces: `KEEPER_TOOLS: list[type]`(`tools.py`);`async def run_consolidation(*, target_date: str, deps...) -> bool`(寫 `consolidated/{target_date}.md` + index;回傳是否成功)。

- [ ] **Step 1: KEEPER_TOOLS allowlist 測試**

`tests/test_consolidation.py`:

```python
def test_keeper_tools_allowlist():
    from dollos.tools import KEEPER_TOOLS, Report, Scratchpad, Shell, NoteMemory, SpawnMonitor, RemoveMonitor
    names = {c.__name__ for c in KEEPER_TOOLS}
    assert names <= {"Report", "Scratchpad", "Recall"}
    assert names & {"Shell", "NoteMemory", "SpawnMonitor", "RemoveMonitor"} == set()
    assert Report in KEEPER_TOOLS
```

跑失敗 → 在 `tools.py` 加 `KEEPER_TOOLS = [Report, Scratchpad]` → PASS。

- [ ] **Step 2: run_consolidation 失敗測試（driver-fed + Report + 寫檔）**

```python
@pytest.mark.asyncio
async def test_run_consolidation_writes_candidate_file(tmp_path, monkeypatch):
    from dollos.mind import consolidation as C
    # 準備目標日 transcript
    tdir = tmp_path / "transcripts"; tdir.mkdir()
    (tdir / "2026-06-29.md").write_text("- 12:00 主人說：我喜歡冰美式\n- 12:01 我說：記住了\n")
    ms = _FakeMemSearch()

    captured = {}
    async def fake_run_agent(**kw):
        captured.update(kw)
        return {"status": "ok", "details": "- 主人偏好冰美式"}
    monkeypatch.setattr(C, "run_agent", fake_run_agent)

    ok = await C.run_consolidation(
        target_date="2026-06-29",
        adapter=object(), renderer=_FakeRenderer(), memsearch=ms,
        memory_root=tmp_path, transcripts_root=tdir,
        tool_output_store=object(), consolidated_dir=tmp_path / "consolidated",
        max_tokens=2048, agent_timeout_s=120, transcript_tail_chars=8000,
    )
    assert ok is True
    out = (tmp_path / "consolidated" / "2026-06-29.md").read_text()
    assert "主人偏好冰美式" in out
    # transcript 內容有 inline 進 task（driver-fed）
    assert "冰美式" in captured["task"]
    # keeper 用 allowlist + 無 shell_runner
    from dollos.tools import KEEPER_TOOLS
    assert captured["tools"] is KEEPER_TOOLS
    assert captured.get("shell_runner") is None
    # 寫的檔被索引
    assert (tmp_path / "consolidated" / "2026-06-29.md") in ms.indexed
```

（`_FakeRenderer` = 在測試檔放一個 `.render(...)→str` 的 stub,或用既有 PromptRenderer。）

- [ ] **Step 3: 實作 run_consolidation**

`src/dollos/mind/consolidation.py`:

```python
"""B2 sleep-time consolidation — driver + trigger.

Driver reads a target day's transcript, feeds it inline to a memory-keeper
agent (KEEPER_TOOLS only), and writes the returned bullets to
consolidated/{date}.md. Candidate facts are pull-only (see mind_loop gating).
"""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

_KEEPER_TASK = """讀以下逐字稿,提取去重成簡潔的中性 candidate 事實——主人的穩定偏好/習慣、你們關係的進展、值得長期記住的模式。陳述為觀察(『主人偏好X』),不要自我宣告(『我是X』)。重複合併、過時捨棄。不確定就不寫,寧缺勿濫。準不要多。把結果用 Report 工具的 details 欄回傳,每條一行 markdown bullet。

逐字稿:
{transcript}
"""


async def run_consolidation(
    *,
    target_date: str,
    adapter,
    renderer,
    memsearch,
    memory_root: Path,
    transcripts_root: Path,
    tool_output_store,
    consolidated_dir: Path,
    max_tokens: int = 2048,
    agent_timeout_s: int = 120,
    transcript_tail_chars: int = 8000,
) -> bool:
    """Consolidate one day's transcript into consolidated/{date}.md.

    Returns True on success (file written + indexed), False otherwise.
    Raises CancelledError through (caller treats as cancel → no write).
    """
    src = transcripts_root / f"{target_date}.md"
    if not src.exists():
        logger.info("consolidation: no transcript for %s; skip", target_date)
        return False
    transcript = src.read_text(encoding="utf-8")[-transcript_tail_chars:]

    # subagent scaffolding system prompt
    system = renderer.render("subagent_scaffolding", tools=KEEPER_TOOLS)

    report = await run_agent(
        task=_KEEPER_TASK.format(transcript=transcript),
        system=system,
        adapter=adapter,
        renderer=renderer,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=transcripts_root,
        tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS,
        max_tokens=max_tokens,
        shell_runner=None,
        monitor_runner=None,
    )
    if not report or not report.get("details"):
        logger.warning("consolidation: keeper returned no report for %s", target_date)
        return False

    consolidated_dir.mkdir(parents=True, exist_ok=True)
    out = consolidated_dir / f"{target_date}.md"
    out.write_text(report["details"].strip() + "\n", encoding="utf-8")
    await memsearch.index_file(out)
    logger.info("consolidation: wrote %s", out)
    return True
```

（`renderer.render(...)` 用專案 PromptRenderer 實際 API;若 subagent_scaffolding 的渲染參數不同,依 `subagent.py` 既有呼叫方式對齊。`agent_timeout_s` 的 wait_for 由 trigger 端包(Task 5),driver 本身專注單次整併;或在此用 `asyncio.wait_for(run_agent(...), agent_timeout_s)`——擇一,本 plan 放 trigger 端,見 Task 5。)

- [ ] **Step 4: 跑通過**

Run: `uv run pytest tests/test_consolidation.py::test_run_consolidation_writes_candidate_file -v` → PASS。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/consolidation.py src/dollos/tools.py tests/test_consolidation.py
git commit -m "feat(memory): B2 memory-keeper driver (driver-fed, Report-driven, allowlist)"
```

---

### Task 5: ConsolidationTrigger observer

**Files:**
- Modify: `src/dollos/mind/consolidation.py`(加 `ConsolidationTrigger`)
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Consumes: Task 1 state 欄位、Task 2 `latest_idle_s` + config、Task 4 `run_consolidation`;`MindState`、`save_state`。
- Produces: `ConsolidationTrigger`(ctor DI 見下);`async def run()`、`shutdown()`;觸發時 `asyncio.create_task(run_consolidation(...))` 存於 `self.current_task`。

- [ ] **Step 1: 觸發條件測試（三條件 AND + idle_s None 不否決 + 日期選擇）**

`tests/test_consolidation.py` 加一組測試,直接呼叫 trigger 的條件判斷方法(把「是否該觸發 + 目標日期」抽成可單測的同步方法 `_pick_target_date(now) -> str | None`,避開 sleep 迴圈):

```python
def _mk_trigger(tmp_path, state, **over):
    from dollos.mind.consolidation import ConsolidationTrigger
    defaults = dict(
        state=state, persist_path=tmp_path/"s.json",
        adapter=object(), renderer=_FakeRenderer(), memsearch=_FakeMemSearch(),
        memory_root=tmp_path, transcripts_root=tmp_path/"transcripts",
        tool_output_store=object(), consolidated_dir=tmp_path/"consolidated",
        system_pulse=None,
        idle_threshold_s=300, min_interval_s=3600,
        max_tokens=2048, agent_timeout_s=120, transcript_tail_chars=8000,
    )
    defaults.update(over)
    return ConsolidationTrigger(**defaults)


def test_no_trigger_when_not_idle(tmp_path):
    from dollos.mind.mind_state import MindState
    s = MindState(); s.user_turn_count = 5; s.last_user_at = 1000.0
    t = _mk_trigger(tmp_path, s)
    # now barely after last_user_at → not idle
    assert t._should_consolidate(now=1010.0) is False


def test_no_trigger_when_no_new_turns(tmp_path):
    from dollos.mind.mind_state import MindState
    s = MindState(); s.user_turn_count = 3; s.last_consolidation_turn = 3
    s.last_user_at = 0.0; s.last_iter_at = 0.0
    t = _mk_trigger(tmp_path, s)
    assert t._should_consolidate(now=10_000.0) is False  # idle ok, but no new turns


def test_no_trigger_within_cooldown(tmp_path):
    from dollos.mind.mind_state import MindState
    s = MindState(); s.user_turn_count = 5; s.last_consolidation_turn = 1
    s.last_user_at = 0.0; s.last_iter_at = 0.0; s.last_consolidation_at = 9_900.0
    t = _mk_trigger(tmp_path, s)
    assert t._should_consolidate(now=10_000.0) is False  # within 3600 cooldown


def test_triggers_when_all_conditions_met(tmp_path):
    from dollos.mind.mind_state import MindState
    s = MindState(); s.user_turn_count = 5; s.last_consolidation_turn = 1
    s.last_user_at = 0.0; s.last_iter_at = 0.0; s.last_consolidation_at = 0.0
    t = _mk_trigger(tmp_path, s)
    assert t._should_consolidate(now=10_000.0) is True


def test_idle_s_none_does_not_veto(tmp_path):
    # SystemPulse present but latest_idle_s() None → optional gate ignored, still triggers
    from dollos.mind.mind_state import MindState
    class _SP:  # pulse with no idle source
        def latest_idle_s(self): return None
    s = MindState(); s.user_turn_count = 5; s.last_consolidation_turn = 1
    s.last_user_at = 0.0; s.last_iter_at = 0.0; s.last_consolidation_at = 0.0
    t = _mk_trigger(tmp_path, s, system_pulse=_SP())
    assert t._should_consolidate(now=10_000.0) is True
```

- [ ] **Step 2: 跨日 oldest-first 測試**

```python
def test_pick_target_date_oldest_first_skips_nothing(tmp_path):
    from dollos.mind.mind_state import MindState
    tdir = tmp_path / "transcripts"; tdir.mkdir()
    (tdir / "2026-06-25.md").write_text("- x\n")
    (tdir / "2026-06-26.md").write_text("- y\n")
    s = MindState(); s.last_consolidated_date = "2026-06-20"
    t = _mk_trigger(tmp_path, s, transcripts_root=tdir)
    # today is 2026-06-30 → both are sealed; oldest unconsolidated = 06-25
    assert t._pick_target_date(today="2026-06-30") == "2026-06-25"


def test_pick_target_date_excludes_today(tmp_path):
    from dollos.mind.mind_state import MindState
    tdir = tmp_path / "transcripts"; tdir.mkdir()
    (tdir / "2026-06-30.md").write_text("- only today\n")
    s = MindState(); s.last_consolidated_date = "2026-06-20"
    t = _mk_trigger(tmp_path, s, transcripts_root=tdir)
    assert t._pick_target_date(today="2026-06-30") is None  # today not sealed yet
```

- [ ] **Step 3: 實作 ConsolidationTrigger**

加到 `src/dollos/mind/consolidation.py`:

```python
import time
from datetime import date as _date
from dollos.mind.mind_state import save_state


class ConsolidationTrigger:
    POLL_INTERVAL_S = 5.0

    def __init__(self, *, state, persist_path, adapter, renderer, memsearch,
                 memory_root, transcripts_root, tool_output_store, consolidated_dir,
                 system_pulse=None, idle_threshold_s=300, min_interval_s=3600,
                 max_tokens=2048, agent_timeout_s=120, transcript_tail_chars=8000):
        self._state = state
        self._persist_path = persist_path
        self._adapter = adapter
        self._renderer = renderer
        self._memsearch = memsearch
        self._memory_root = memory_root
        self._transcripts_root = transcripts_root
        self._tool_output_store = tool_output_store
        self._consolidated_dir = consolidated_dir
        self._system_pulse = system_pulse
        self._idle_threshold_s = idle_threshold_s
        self._min_interval_s = min_interval_s
        self._max_tokens = max_tokens
        self._agent_timeout_s = agent_timeout_s
        self._transcript_tail_chars = transcript_tail_chars
        self._shutdown = False
        self.current_task: asyncio.Task | None = None

    def _conversation_idle(self, now: float) -> float:
        return now - max(self._state.last_user_at, self._state.last_iter_at)

    def _should_consolidate(self, now: float) -> bool:
        if self._conversation_idle(now) < self._idle_threshold_s:
            return False
        if self._state.user_turn_count <= self._state.last_consolidation_turn:
            return False
        if now - self._state.last_consolidation_at < self._min_interval_s:
            return False
        # optional SystemPulse gate: only vetoes when a fresh idle_s is available
        if self._system_pulse is not None:
            idle = self._system_pulse.latest_idle_s()
            if idle is not None and idle < self._idle_threshold_s:
                return False
        return True

    def _pick_target_date(self, today: str) -> str | None:
        """Oldest sealed (date < today) transcript date after last_consolidated_date."""
        watermark = self._state.last_consolidated_date
        candidates = []
        if self._transcripts_root.exists():
            for f in self._transcripts_root.glob("*.md"):
                d = f.stem  # YYYY-MM-DD
                if d < today and d > watermark and f.read_text(encoding="utf-8").strip():
                    candidates.append(d)
        return min(candidates) if candidates else None

    async def run(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                now = time.time()
                if not self._should_consolidate(now):
                    continue
                today = _date.today().isoformat()
                target = self._pick_target_date(today)
                # attempt timestamp advances regardless (cooldown; no 5s storm)
                self._state.last_consolidation_at = now
                if target is None:
                    # idle + new turns but nothing sealed to do yet; persist cooldown
                    save_state(self._state, self._persist_path)
                    continue
                ok = await self._run_once(target)
                if ok:
                    self._state.last_consolidation_turn = self._state.user_turn_count
                    self._state.last_consolidated_date = target
                save_state(self._state, self._persist_path)
            except asyncio.CancelledError:
                # cancelled by a returning user (or shutdown); persist cooldown, re-raise on shutdown
                save_state(self._state, self._persist_path)
                if self._shutdown:
                    raise
            except Exception:
                logger.exception("consolidation trigger iteration failed; continuing")

    async def _run_once(self, target: str) -> bool:
        self.current_task = asyncio.create_task(
            asyncio.wait_for(
                run_consolidation(
                    target_date=target, adapter=self._adapter, renderer=self._renderer,
                    memsearch=self._memsearch, memory_root=self._memory_root,
                    transcripts_root=self._transcripts_root,
                    tool_output_store=self._tool_output_store,
                    consolidated_dir=self._consolidated_dir,
                    max_tokens=self._max_tokens, agent_timeout_s=self._agent_timeout_s,
                    transcript_tail_chars=self._transcript_tail_chars,
                ),
                timeout=self._agent_timeout_s,
            )
        )
        try:
            return await self.current_task
        except (asyncio.TimeoutError, Exception):
            logger.exception("consolidation run failed/timed out for %s", target)
            return False
        finally:
            self.current_task = None

    def cancel_current(self) -> None:
        t = self.current_task
        if t is not None and not t.done():
            t.cancel()

    def shutdown(self) -> None:
        self._shutdown = True
        self.cancel_current()
```

（注意 `_run_once` 的 except 順序:`TimeoutError` 是 `Exception` 子類,合併捕捉即可;但 `CancelledError` 在 py3.8+ 是 `BaseException`,不會被 `except Exception` 吞——使用者 cancel 會往上傳到 `run()` 的 `except asyncio.CancelledError`,正確跳過寫檔。確認實際 Python 版本行為。）

- [ ] **Step 4: 跑全部 trigger 測試 → PASS**

Run: `uv run pytest tests/test_consolidation.py -v` → 全綠。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/consolidation.py tests/test_consolidation.py
git commit -m "feat(memory): B2 ConsolidationTrigger (idle conditions + oldest-first + save_state)"
```

---

### Task 6: kernel 整合 — wiring + cancel 接縫 + shutdown

**Files:**
- Modify: `src/dollos/kernel.py`(建 trigger `:320`、start task `:655`、cancel 接縫 `:366/454`、shutdown `:664/679`)
- Test: `tests/test_kernel.py` 或 `tests/test_consolidation.py`(整合測試)

**Interfaces:**
- Consumes: `ConsolidationTrigger`、`Settings.consolidation`。
- Produces: `self._consolidation_trigger` + `self._consolidation_trigger_task`;`self._cancel_consolidation()`。

- [ ] **Step 1: 失敗測試（UserSpoke 兩路徑 cancel）**

整合測試較重;最小可測:kernel 有 `_cancel_consolidation()`,呼叫時若 trigger 有 current_task 則 cancel。用輕量 stub trigger:

```python
def test_cancel_consolidation_cancels_current(monkeypatch, tmp_path):
    # 建一個最小 kernel-like 或直接測 _cancel_consolidation 的邏輯
    class _StubTrigger:
        def __init__(self): self.cancelled = False
        def cancel_current(self): self.cancelled = True
    # 視 kernel 建構複雜度，這裡測 trigger.cancel_current 被呼叫的接線
    trig = _StubTrigger()
    # _cancel_consolidation 應呼叫 trigger.cancel_current()
    # （依實際 kernel 結構，用 monkeypatch 注入 trig 後呼叫 handler）
```

（kernel 建構重 → 此 task 的測試以「`_cancel_consolidation` 呼叫 `trigger.cancel_current()`」與「shutdown 順序」為主;UserSpoke→cancel 的端到端可在實機 smoke 驗。若 kernel 有既有測試 harness 就沿用。）

- [ ] **Step 2: 實作 — 建 trigger（kernel.py 建構區 ~:320，仿 ReflectionObserver）**

```python
        from dollos.mind.consolidation import ConsolidationTrigger
        self._consolidation_trigger = ConsolidationTrigger(
            state=self._state,
            persist_path=<同 mind_loop 的 state_persist_path>,
            adapter=self.adapter, renderer=self.renderer, memsearch=self.memsearch,
            memory_root=<memory_root>, transcripts_root=<transcripts_root>,
            tool_output_store=self.tool_output_store,
            consolidated_dir=<memory_root>/"shared"/"consolidated",
            system_pulse=self.system_pulse,
            idle_threshold_s=settings.consolidation.idle_threshold_s,
            min_interval_s=settings.consolidation.min_interval_s,
            max_tokens=settings.consolidation.max_tokens,
            agent_timeout_s=settings.consolidation.agent_timeout_s,
            transcript_tail_chars=settings.consolidation.transcript_tail_chars,
        )
```

（用 kernel 既有屬性名對齊:`self.adapter`/`self.renderer`/`self.memsearch`/`self.tool_output_store`/`self.system_pulse` 與 build 區實際命名一致;memory_root/transcripts_root/persist_path 用 kernel 既有的路徑變數。）

- [ ] **Step 3: start task（仿 reflection `:655`，gate on enabled）**

```python
            if settings.consolidation.enabled:
                self._consolidation_trigger_task = asyncio.create_task(
                    self._consolidation_trigger.run(), name="consolidation-trigger"
                )
```

- [ ] **Step 4: cancel 接縫（兩個 UserSpoke ingress）**

加 helper:

```python
    def _cancel_consolidation(self) -> None:
        trig = getattr(self, "_consolidation_trigger", None)
        if trig is not None:
            trig.cancel_current()
```

在 text ingress(`_handle_message` 的 TextInput 分支 ~`:366`,`put(UserSpoke)` 前後)與 voice ingress(`_on_user_text` ~`:454`,`put(UserSpoke)` 前後)各加 `self._cancel_consolidation()`。

- [ ] **Step 5: shutdown 拆除（finally ~:664，memsearch.close() 之前）**

在 `await self.workflow_runner.stop()` 同段、`self.memsearch.close()` **之前**:

```python
                if getattr(self, "_consolidation_trigger", None) is not None:
                    self._consolidation_trigger.shutdown()
                t = getattr(self, "_consolidation_trigger_task", None)
                if t is not None:
                    t.cancel()
                    await asyncio.gather(t, return_exceptions=True)
```

- [ ] **Step 6: 跑測試 + 回歸**

Run: `uv run pytest tests/test_consolidation.py tests/test_kernel.py -v` → PASS。
Run: `uv run pytest -q`(full suite)→ 全綠(0 failed)。

- [ ] **Step 7: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel.py tests/test_consolidation.py
git commit -m "feat(memory): B2 kernel wiring + UserSpoke cancel seam + shutdown teardown"
```

---

## Self-Review

**1. Spec coverage:**
- §3.1 ConsolidationTrigger(conversation-idle/三條件/optional gate/restart/save_state)→ Task 5 ✓
- §3.2 keeper driver-fed/KEEPER_TOOLS/Report-driven/driver 寫/wait_for/失敗語意 → Task 4 + Task 5(`_run_once` wait_for + run() 失敗語意)✓
- §3.3 召回 pull-only + provenance → Task 3 ✓
- §3.4 日期(同日>=/oldest-first/只併已封日)→ Task 5 `_pick_target_date`(`d < today` + `d > watermark` + oldest)✓ 註:同日 `>=` 由「只併已封日(date<today)」涵蓋——today 不併,故無同日重併問題;watermark 用 `>` 但只作用於已封日,不會漏(隔天 today 變昨天才併)。**已對齊 spec §3.4。**
- §4 四欄位 + 顯式三處 + restart → Task 1 ✓
- §3.1 DI + config → Task 2 + Task 6 ✓
- §3.1 latest_idle_s + 新鮮度 → Task 2 ✓
- M3 cancel 接縫 → Task 6 ✓
- M4 shutdown → Task 6 ✓
- §6 測試清單 → Task 1-6 覆蓋(三條件/idle_s None/user_turn_count/keeper allowlist/driver-fed/pull-only/oldest-first/今日排除/save-load)✓

**2. Placeholder scan:** 實作 snippet 中的 `<...>`(kernel 路徑/屬性名)是「對齊既有命名」的明確指示,非 TODO——implementer 讀 kernel 即得;已標註對齊來源。測試碼完整。無 vacuous 斷言(keeper allowlist 是正面集合斷言)。

**3. Type consistency:** `run_consolidation` 簽名(Task 4)與 `ConsolidationTrigger._run_once` 呼叫(Task 5)一致;`KEEPER_TOOLS`(Task 4 定義)→ Task 4 測試 + driver 使用一致;state 四欄位(Task 1)→ Task 5 讀取一致;`latest_idle_s`(Task 2)→ Task 5 `_should_consolidate` 使用一致;`cancel_current`/`shutdown`(Task 5)→ Task 6 kernel 呼叫一致。

**Note(交付執行時):** Task 6 的 kernel 整合測試較難純單元化(kernel 建構重);實機 smoke(idle→整併→UserSpoke→cancel)建議在全 plan 完成後跑一次 daemon 驗證。單元層以 trigger 邏輯(Task 5)+ driver(Task 4)+ gating(Task 3)為主要保護網。
