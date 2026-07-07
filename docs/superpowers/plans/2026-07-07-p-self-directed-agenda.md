# Self-Directed Agenda — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Doll 持有並在空檔自主推進她自己的議程項目(從「持有的興趣」到「主動推進的目標」),錨定真實觸發、energy+idle+throttle 有界、reactive 永遠優先、v1 只用內部認知子集。

**Architecture:** 重用既有零件(`open_loops` 容器 / `ReflectionObserver` 樣板 / energy 系統 / `PendingEvent` / trace)。新增:`OpenLoop` 加 self-directed 欄位 + auto-provenance;`PursueGoal`/`AdvanceGoal` 工具(genesis 只在 reflection/reactive turn);`[Your agenda]` 渲染;reflection grounded nudge;`AgendaObserver`(四閘)發 `AgendaMoment`;`_is_agenda` flag + pure-agenda registry 分支(強制 `AGENDA_TOOLS`,排除 user-present co-batch)。

**Tech Stack:** Python 3.13 / asyncio / pydantic v2 / dataclass MindState / pytest。全在 `src/dollos/`。

**Spec:** `docs/superpowers/specs/2026-07-07-self-directed-agenda-design.md`(R1 opus 對抗硬化;1 Critical + 5 Important + 4 Minor 已折入)。每個 task 開工前讀對應 spec 章節。

## Global Constraints

- **錨定真實不變式(spec §3.3)**:每個 self-directed 議程項目必須能追溯到真實觸發。執行:`PursueGoal` required `trigger`(她自述 why)**＋ code 自動捕捉的 `provenance`(模型寫不到:turn id + 當回合真實 `[Memory context]` 命中 source + iter)**。稽核基準是 `provenance`(非 `trigger`),且**排除 Doll 自己自主寫的記憶**。軟機制、靠稽核,不假裝不可捏。
- **reactive 永遠優先**:energy floor 只閘自主 turn、永不閘 reactive;in-flight 自主 cascade 被返回使用者 preempt(既有 `kernel.py:954-979`);idle 閘保證只在沒人找時自發。
- **v1 自主 turn 只認知子集**:`AGENDA_TOOLS = {Recall, AdvanceGoal, CloseLoop, MoodTool}`。**排除** Shell/SpawnWorkflow/SpawnMonitor/WriteSchedule/SelfRevision/PinSelf/**NoteMemory**(R1-I1 自我 bootstrap)/**PursueGoal**(R1-I3 genesis 移出自主 turn)。**structurally enforced via registry availability**(非 post-hoc)。
- **C1 co-batch 不 whitewash 使用者**:`_is_agenda = any(AgendaMoment) and not _has_user_spoke` —— 有 UserSpoke 同 batch → 非純 agenda turn → 保留全 registry(鏡射既有 MF-2 LearnName-C1 防線)。
- **自主 turn 不對外發話**(R1-M1);**有界成長**:`progress` cap `_MAX_PROGRESS=8`、self_directed loop 數 cap `_MAX_SELF_LOOPS=12`。
- **throttle 是節奏主邊界**(R1-I3:energy 管不住純思考 turn):AgendaObserver 距上次發至少 `_AGENDA_MIN_INTERVAL_S`(~5-10 分鐘)。
- **No fallback / 向後相容**:`OpenLoop` 加欄位皆有預設(舊資料 `_coerce` 填);既有 `self_directed=False` loop / reflection 行為不變。

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/dollos/mind/mind_state.py` | Modify | `OpenLoop` 加 `self_directed/trigger/provenance/progress`(有界)+ `_MAX_*` 常數;`Perception.kind` Literal 加 `"AgendaMoment"` |
| `src/dollos/tools.py` | Modify | `PursueGoal`(required trigger + auto-provenance)+ `AdvanceGoal`(有界 append);`AGENDA_TOOLS` frozenset |
| `src/dollos/mind/mind_prompt.py` | Modify | `[Your agenda]` 渲染(self_directed)+ `[Open loops]` 只 user-owed;reflection grounded nudge |
| `src/dollos/mind/agenda_observer.py` | Create | `AgendaObserver`(idle+floor+has-loop+throttle 四閘 → `AgendaMoment`) |
| `src/dollos/mind/mind_loop.py` | Modify | `_is_agenda` flag + pure-agenda registry 分支(`AGENDA_TOOLS`)+ 自主 turn streamed-text 抑制 |
| `src/dollos/kernel.py` | Modify | 建 `AgendaObserver` + 併背景 task 群 + 關機收 |

依序:**Task 1(資料)→ 2(工具)→ 3(渲染)→ 4(nudge)→ 5(observer + registry 強制,承重)→ 6(推進 turn)**。

---

### Task 1: OpenLoop 資料模型 + AgendaMoment Literal

**Files:**
- Modify: `src/dollos/mind/mind_state.py`(`OpenLoop` ~66;`Perception.kind` Literal ~74-79)
- Test: `tests/test_mind_state_agenda.py`(new)

**Interfaces:**
- Produces: `OpenLoop(id, desc, opened_at, self_directed=False, trigger="", provenance={}, progress=[])`;module const `_MAX_PROGRESS=8`、`_MAX_SELF_LOOPS=12`;`Perception.kind` 含 `"AgendaMoment"`。

- [ ] **Step 1: 寫 failing test**
```python
from dataclasses import asdict
from dollos.mind.mind_state import OpenLoop, MindState, save_state, load_state


def test_openloop_new_fields_default():
    ol = OpenLoop(id="x", desc="d", opened_at=1.0)
    assert ol.self_directed is False and ol.trigger == "" and ol.provenance == {} and ol.progress == []


def test_openloop_backward_compat_missing_fields(tmp_path):
    # old persisted state has open_loops without the new fields → _coerce fills defaults
    import json
    p = tmp_path / "mind_state.json"
    p.write_text(json.dumps({"open_loops": [{"id": "old", "desc": "d", "opened_at": 1.0}]}))
    st = load_state(p)
    assert st.open_loops[0].self_directed is False and st.open_loops[0].provenance == {}


def test_openloop_roundtrip_with_new_fields(tmp_path):
    st = MindState()
    st.open_loops.append(OpenLoop(id="g", desc="pursue", opened_at=2.0, self_directed=True,
                                  trigger="from chat", provenance={"turn_id": "5"}, progress=["step1"]))
    p = tmp_path / "s.json"; save_state(st, p)
    back = load_state(p)
    assert back.open_loops[0].self_directed is True
    assert back.open_loops[0].provenance == {"turn_id": "5"} and back.open_loops[0].progress == ["step1"]


def test_agenda_moment_in_perception_kind_literal():
    import typing, dollos.mind.mind_state as ms
    kinds = typing.get_args(ms.Perception.__dataclass_fields__["kind"].type)
    # Literal args; AgendaMoment must be present
    assert "AgendaMoment" in typing.get_args(kinds[0]) or "AgendaMoment" in kinds
```
> 註:`Perception.kind` 的 Literal 取值方式依實際型別註解;實作者用 `test_mind_state.py` 既有讀法對齊。

- [ ] **Step 2: RED** — `uv run pytest tests/test_mind_state_agenda.py -v`(FAIL:欄位/kind 不存在)

- [ ] **Step 3: 實作** — `mind_state.py`:
```python
_MAX_PROGRESS = 8
_MAX_SELF_LOOPS = 12


@dataclass
class OpenLoop:
    id: str
    desc: str
    opened_at: float
    self_directed: bool = False
    trigger: str = ""
    provenance: dict = field(default_factory=dict)   # code-filled at genesis; model cannot write
    progress: list = field(default_factory=list)
```
`Perception.kind` Literal 加 `"AgendaMoment"`。確認 `field` 已 import(既有 dataclass 用)。`_coerce`(`:284` 一帶)對缺欄位填預設 —— 確認它用 dataclass 欄位預設(若 `_coerce` 是逐欄位取,補新欄位的容錯)。

- [ ] **Step 4: GREEN** — `uv run pytest tests/test_mind_state_agenda.py -v` + `uv run pytest tests/test_mind_state.py -q`(迴歸)

- [ ] **Step 5: Commit** — `git commit -m "feat(agenda): OpenLoop self-directed fields + AgendaMoment kind [Task 1]"`

---

### Task 2: PursueGoal / AdvanceGoal 工具 + AGENDA_TOOLS

**Files:**
- Modify: `src/dollos/tools.py`(near `OpenLoop`/`CloseLoop` ~724-761;工具註冊表;`AGENDA_TOOLS`)
- Test: `tests/test_tools_agenda.py`(new)

**Interfaces:**
- Consumes: `OpenLoop`(Task 1,含新欄位)、`MindCtx`(有 `current_turn` iter + 當回合 memory-hit sources access)。
- Produces: `PursueGoal(id, desc, trigger)`(建 self_directed loop + auto-provenance)、`AdvanceGoal(id, progress)`(有界 append);`AGENDA_TOOLS: frozenset = {"Recall","AdvanceGoal","CloseLoop","MoodTool"}`。

- [ ] **Step 1: 讀 spec §3.1/§3.3/§5.1**(auto-provenance 是 code 填非模型;AGENDA_TOOLS 排除清單)。

- [ ] **Step 2: 寫 failing test**
```python
import pytest
from pydantic import ValidationError
from dollos.tools import PursueGoal, AdvanceGoal, AGENDA_TOOLS


def test_pursuegoal_trigger_required():
    with pytest.raises(ValidationError):
        PursueGoal(id="g", desc="d")  # no trigger


@pytest.mark.asyncio
async def test_pursuegoal_creates_self_directed_with_auto_provenance(fake_ctx_with_memory_hits):
    ctx = fake_ctx_with_memory_hits  # ctx.current_turn set + ctx has real [Memory context] hit sources
    await PursueGoal(id="g", desc="explore X", trigger="the chat about X").run(ctx)
    ol = next(l for l in ctx.mind_state.open_loops if l.id == "g")
    assert ol.self_directed is True and ol.trigger == "the chat about X"
    # provenance is CODE-captured from ctx, NOT from the tool args
    assert ol.provenance.get("turn_id") is not None
    # memory_sources reflect what was actually in context (proves non-fabricable grounding)
    assert "memory_sources" in ol.provenance


@pytest.mark.asyncio
async def test_advancegoal_appends_bounded(fake_ctx):
    from dollos.mind.mind_state import OpenLoop, _MAX_PROGRESS
    fake_ctx.mind_state.open_loops.append(OpenLoop(id="g", desc="d", opened_at=1.0, self_directed=True))
    for i in range(_MAX_PROGRESS + 3):
        await AdvanceGoal(id="g", progress=f"step{i}").run(fake_ctx)
    ol = next(l for l in fake_ctx.mind_state.open_loops if l.id == "g")
    assert len(ol.progress) == _MAX_PROGRESS  # bounded


def test_agenda_tools_excludes_dangerous_and_genesis():
    for excluded in ("Shell", "SpawnWorkflow", "SelfRevision", "NoteMemory", "PursueGoal", "PinSelf", "WriteSchedule"):
        assert excluded not in AGENDA_TOOLS
    assert AGENDA_TOOLS == frozenset({"Recall", "AdvanceGoal", "CloseLoop", "MoodTool"})
```
> 實作者:`fake_ctx`/`fake_ctx_with_memory_hits` 依既有 tools 測試的 ctx fixture 造(`MindCtx` + mind_state + current_turn + 一個帶 memory-hit sources 的 turn 狀態)。auto-provenance 的來源(ctx 上哪個欄位存當回合 memory-hit sources)由實作者 grep 既有 `_derive_memory_hits`/trace 組裝(`mind_loop.py:621-623` trace_blocks)確認;若 ctx 尚未攜帶 memory-hit sources,Task 2 需在 ctx 上暴露一個唯讀 accessor(不新增管線,只讀既有 turn 狀態)。

- [ ] **Step 3: RED** — `uv run pytest tests/test_tools_agenda.py -v`

- [ ] **Step 4: 實作** — `tools.py`:
```python
class PursueGoal(BaseModel):
    """開一條你自己想追的線(不是欠誰的 TODO,是你自己在意/好奇的)。"""
    id: str = Field(..., description="short slug id")
    desc: str = Field(..., description="你想追什麼")
    trigger: str = Field(..., description="這是從哪來的?——引用剛剛的對話/一段記憶/一個真實經歷。必填。")

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import OpenLoop as OpenLoopT
        prov = {
            "turn_id": str(getattr(ctx, "current_turn", "")),
            "opened_iter": ctx.mind_state.iter_count,
            "memory_sources": list(ctx.recent_memory_sources()),  # code-captured; model can't write
        }
        ctx.mind_state.open_loops.append(OpenLoopT(
            id=self.id, desc=self.desc, opened_at=time.time(),
            self_directed=True, trigger=self.trigger, provenance=prov, progress=[]))
        _record(ctx, "PursueGoal", f"pursue {self.id}: {self.desc[:50]}")
        return f"opened self-directed loop {self.id}"


class AdvanceGoal(BaseModel):
    """在你自己的議程上記一步進展 / 洞察。"""
    id: str = Field(..., description="which agenda loop")
    progress: str = Field(..., description="a concrete step or insight")

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind.mind_state import _MAX_PROGRESS
        for ol in ctx.mind_state.open_loops:
            if ol.id == self.id and ol.self_directed:
                ol.progress = (ol.progress + [self.progress])[-_MAX_PROGRESS:]
                _record(ctx, "AdvanceGoal", f"advance {self.id}: {self.progress[:50]}")
                return f"advanced {self.id}"
        return f"no self-directed loop {self.id}"


AGENDA_TOOLS: frozenset[str] = frozenset({"Recall", "AdvanceGoal", "CloseLoop", "MoodTool"})
```
註冊 `PursueGoal`/`AdvanceGoal` 進工具表(`PursueGoal` 進 reflection/reactive 可用的表,**不進** `AGENDA_TOOLS`;`AdvanceGoal` 在 `AGENDA_TOOLS` 內)。`ctx.recent_memory_sources()` 若不存在,加一個唯讀 helper 讀既有 turn 的 memory-hit sources。

- [ ] **Step 5: GREEN + 迴歸** — `uv run pytest tests/test_tools_agenda.py -v` + `uv run pytest -q`

- [ ] **Step 6: Commit** — `git commit -m "feat(agenda): PursueGoal(auto-provenance)/AdvanceGoal tools + AGENDA_TOOLS subset [Task 2]"`

---

### Task 3: `[Your agenda]` 渲染

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`(`_render_open_loops` ~330;`[Open loops]` block ~144)
- Test: `tests/test_mind_prompt_agenda.py`(new)

**Interfaces:**
- Consumes: `state.open_loops`(含 `self_directed`/`trigger`/`progress`)。
- Produces: `[Your agenda]` block render(self_directed)+ `[Open loops]` 只渲染 user-owed。

- [ ] **Step 1: 寫 failing test** — self_directed loop 出現在 `[Your agenda]`(含 trigger + 最近 progress)、不在 `[Open loops]`;user-owed(self_directed=False)相反。
```python
def test_self_directed_renders_in_your_agenda_not_open_loops():
    from dollos.mind.mind_state import MindState, OpenLoop
    st = MindState()
    st.open_loops = [
        OpenLoop(id="a", desc="my curiosity", opened_at=1.0, self_directed=True,
                 trigger="chat about X", progress=["p1"]),
        OpenLoop(id="b", desc="owed TODO", opened_at=1.0, self_directed=False),
    ]
    from dollos.mind.mind_prompt import _render_your_agenda, _render_open_loops
    agenda = _render_your_agenda(st.open_loops, now=2.0)
    loops = _render_open_loops([l for l in st.open_loops if not l.self_directed], now=2.0)
    assert "my curiosity" in agenda and "chat about X" in agenda and "p1" in agenda
    assert "my curiosity" not in loops and "owed TODO" in loops
```

- [ ] **Step 2: RED** → FAIL(`_render_your_agenda` 不存在)

- [ ] **Step 3: 實作** — 加 `_render_your_agenda(loops, now)`(只取 `self_directed`,每項 `desc` + `trigger` + 最近 progress 尾);把它併入 prompt 組裝(`[Your agenda] (things you're pursuing because you want to)`);既有 `[Open loops]` 的 `_render_open_loops` 呼叫改傳 `[l for l in loops if not l.self_directed]`(只 user-owed)。

- [ ] **Step 4: GREEN + 迴歸** → `uv run pytest tests/test_mind_prompt_agenda.py tests/test_mind_prompt*.py -q`

- [ ] **Step 5: Commit** — `git commit -m "feat(agenda): [Your agenda] render split from [Open loops] [Task 3]"`

---

### Task 4: reflection grounded nudge

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py`(ReflectionMoment turn 的 prompt 組裝處)
- Test: `tests/test_mind_prompt_reflection_nudge.py`(new)

**Interfaces:**
- Consumes: 「這是 reflection turn」的訊號(既有 `_is_reflection` / perception kind)。
- Produces: reflection turn 的 prompt 含 grounded nudge;非 reflection 不含。

- [ ] **Step 1: 讀 spec §3.2**(nudge 措辭「回顧真實近期、沒有就算了」,非「你有目標去追」)。

- [ ] **Step 2: 寫 failing test** — 帶 ReflectionMoment 的 prompt 含 nudge 措辭(如「回顧」「自己想追」);不帶時不含。找既有 reflection prompt 組裝測試(grep `ReflectionMoment`/reflection nudge in tests)鏡射。

- [ ] **Step 3: RED**

- [ ] **Step 4: 實作** — 在 reflection turn 的 prompt 組裝加(spec §3.2 措辭):
> 「回顧你最近**真的**碰到、好奇、或在意的——有沒有哪條線是你**自己**想追下去的?(有就用 `PursueGoal` 記下,說清楚它從哪來。沒有就算了,不用硬找。)」
只在 reflection turn 加(既有 `_is_reflection` gate)。

- [ ] **Step 5: GREEN + 迴歸**

- [ ] **Step 6: Commit** — `git commit -m "feat(agenda): grounded self-directed nudge on reflection turns [Task 4]"`

---

### Task 5: AgendaObserver + AgendaMoment + registry 強制(承重,R1-C1)

**Files:**
- Create: `src/dollos/mind/agenda_observer.py`
- Modify: `src/dollos/mind/mind_loop.py`(`_is_agenda` flag ~385 旁;`_active_tool_registry` ~862-868 加分支;streamed-text 抑制);`src/dollos/kernel.py`(建 observer + 背景 task + 關機收,鏡射 `ReflectionObserver` @`kernel.py:582`);`src/dollos/config.py`(可選 `AgendaConfig`,或先模組常數)
- Test: `tests/test_agenda_observer.py`、`tests/test_mind_loop_agenda_registry.py`(new)

**Interfaces:**
- Consumes: `MindState`(`energy`/`last_user_at`/`open_loops`/`iter_count`)、`EnergyConfig`(idle_threshold_s)、`PerceptionQueue`。
- Produces: `AgendaObserver`(run/shutdown,鏡射 `ReflectionObserver`);`AgendaMoment` perception;`_is_agenda` flag;pure-agenda turn → `AGENDA_TOOLS`。

- [ ] **Step 1: 讀 spec §4.1/§5.2**(四閘;registry 分支 order + co-batch)。

- [ ] **Step 2: 寫 failing tests — observer 四閘**
```python
# 鏡射 tests 對 ReflectionObserver 的驅動法（poll + queue 斷言）
# idle 不足 / energy<=floor / 無 active self-directed loop / throttle 未到 → 各自不發 AgendaMoment
# 四閘全過 → 發一個 AgendaMoment；發後 throttle 內不再發
```
**registry 強制 tests(承重,R1-C1)**：
```python
# 純 AgendaMoment turn（無 UserSpoke）→ _active_tool_registry() == 以 AGENDA_TOOLS 為鍵的子集
#   （不含 Shell/SpawnWorkflow/SelfRevision/NoteMemory/PursueGoal）
# AgendaMoment + UserSpoke co-batch → 保留全 registry（使用者請求不被限制）— 鏡射既有 MF-2 測法
```

- [ ] **Step 3: RED**

- [ ] **Step 4: 實作 `agenda_observer.py`**(鏡射 `reflection_observer.py:19`):
```python
_POLL_INTERVAL_S = 30.0
_AGENDA_ENERGY_FLOOR = 0.5
_AGENDA_MIN_INTERVAL_S = 420.0   # ~7 min throttle (R1-I3, primary bound)

class AgendaObserver:
    """Fires AgendaMoment when idle + energized + has active self-directed loop + throttle passed."""
    def __init__(self, *, state, queue, energy_idle_threshold_s: float):
        self._state = state; self._queue = queue
        self._idle_threshold = energy_idle_threshold_s
        self._last_fire_at = 0.0; self._shutdown = False

    async def run(self):
        import time
        self._last_fire_at = time.time()   # don't fire immediately on boot
        while not self._shutdown:
            await asyncio.sleep(_POLL_INTERVAL_S)
            now = time.time()
            idle = (now - self._state.last_user_at) > self._idle_threshold
            energized = self._state.energy > _AGENDA_ENERGY_FLOOR
            has_loop = any(l.self_directed for l in self._state.open_loops)
            throttled = (now - self._last_fire_at) < _AGENDA_MIN_INTERVAL_S
            if idle and energized and has_loop and not throttled:
                self._queue.put(Perception(kind="AgendaMoment", t=now, data={}))
                self._last_fire_at = now

    def shutdown(self):
        self._shutdown = True
```
**`mind_loop.py`** — set flag(鏡射 `_is_reflection` @379):
```python
        self._is_agenda = (any(p.kind == "AgendaMoment" for p in perceptions)
                           and not self._has_user_spoke)   # co-batch w/ UserSpoke → NOT pure agenda (C1)
```
`_active_tool_registry` — 加分支,**排在 `_is_reflection` 之後、`_has_user_spoke` fall-through 之前**(現 `mind_loop.py:862-868`):
```python
        if self._is_reflection:
            ...   # (unchanged)
        if self._is_agenda:   # pure agenda turn (origin_tier=="internal" by position; not _has_user_spoke by flag)
            return {n: c for n, c in self._tool_registry.items() if n in AGENDA_TOOLS}
        if self._has_user_spoke:
            ...
```
> 因 `_is_agenda` 已含 `and not self._has_user_spoke`,co-batch(有 UserSpoke)→ `_is_agenda=False` → 落到 `_has_user_spoke` 分支 → 全 registry。純 agenda(無 UserSpoke)→ AGENDA_TOOLS。reflection+agenda co-batch → reflection 分支先 return(可接受,reflection 語意)。`AGENDA_TOOLS` 從 `tools.py` import。
**streamed-text 抑制**(R1-M1):AgendaMoment turn 的 streamed text 不進任何 sink(她內心思考)——在 `_emit_sentence`/emit 路徑對 `self._is_agenda` turn 丟棄(或只入 trace)。實作者對照 §5.3。
**`kernel.py`** — 鏡射 `ReflectionObserver`(`kernel.py:582` 建構 + 背景 task 群 + 關機 shutdown+gather)建 `AgendaObserver`。

- [ ] **Step 5: GREEN + 迴歸** — `uv run pytest tests/test_agenda_observer.py tests/test_mind_loop_agenda_registry.py -v` + `uv run pytest -q`(**MF-2/LearnName registry 測試不得破**)

- [ ] **Step 6: Commit** — `git commit -m "feat(agenda): AgendaObserver 4-gate + AgendaMoment + pure-agenda registry (AGENDA_TOOLS, co-batch-safe) [Task 5]"`

---

### Task 6: 推進 turn 語意 + energy

**Files:**
- Modify: (多在 Task 5 已接;本 task 驗證 end-to-end turn 語意 + energy 扣)
- Test: `tests/test_mind_loop_agenda_turn.py`(new)

**Interfaces:**
- Consumes: `AgendaMoment` perception → mind_loop turn(AGENDA_TOOLS registry)。
- Produces: 一 AgendaMoment turn 挑一項 self_directed loop、`AdvanceGoal` append 一步、energy 在有 tool 時扣。

- [ ] **Step 1: 寫 failing test**
```python
# 驅動一個 AgendaMoment turn（fake LLM 回一個 AdvanceGoal tool call on an existing self_directed loop）
# → 該 loop.progress append 一條；energy 扣 cost_per_turn（因有 tool = produced）
# 純思考 turn（fake LLM 只 think 無 tool、無 speech）→ energy 不扣（斷言 → 證明 throttle 才是邊界）
# AgendaMoment turn 的 streamed text 不進 sink（她不對外發話，R1-M1）
```

- [ ] **Step 2: RED**

- [ ] **Step 3: 實作 / 接線** — 大多在 Task 5 已具備;本 task 補齊 turn 走既有 cascade 的細節(AgendaMoment 是 origin-less internal perception,走 `_llm_iterate`;確認 energy 扣走既有 `mind_loop.py:664` `produced` 路;streamed-text 抑制 Task 5 已加)。

- [ ] **Step 4: GREEN + 迴歸 + Live-smoke 記載** — `uv run pytest -q`。Live-smoke(§9,人工):真 daemon idle>10min + energy>floor + 有 self-directed loop → 觀察她**偶爾**(throttle)自發推進;trace 稽核 provenance。**人工步驟,不在此執行**。

- [ ] **Step 5: Commit** — `git commit -m "feat(agenda): AgendaMoment pursuit turn semantics + energy drain [Task 6]"`

---

## Self-Review

**1. Spec coverage:** §2 資料→T1;§3.1/§3.3 genesis+provenance→T2;§6.1 render→T3;§3.2 nudge→T4;§4.1 observer 四閘 + §5.2 C1 registry→T5;§4.3 推進 turn+energy→T6。§7 安全(AGENDA_TOOLS 排除、co-batch、provenance、caps、throttle)分佈 T1/T2/T5。§11 開放決策(floor/idle/throttle/mood/self-talk/nudge 頻率)= 常數,dogfood 調。✓

**2. Placeholder scan:** T2 的 `ctx.recent_memory_sources()` / T4 找既有 reflection nudge 測試 / T5 streamed-text 抑制點 —— 皆指向具體既有 code 讓實作者 grep 定位(auto-provenance 來源、reflection prompt 組裝、emit 路徑),非 vague TODO;spec §3.1/§3.2/§5.3 有完整 rationale。

**3. Type consistency:** `OpenLoop` 欄位(self_directed/trigger/provenance/progress)T1 定義、T2/T3 使用一致;`AGENDA_TOOLS` frozenset T2 定義、T5 使用;`_is_agenda`(`and not _has_user_spoke`)T5 一致;`AgendaMoment` kind T1 定義、T5/T6 使用;`_MAX_PROGRESS`/`_MAX_SELF_LOOPS` T1 定義、T2 使用。✓

**承重**:Task 5(observer + C1 registry 強制 + co-batch)、Task 2(auto-provenance 錨定真實)、Task 6(推進語意)承重 → **opus 審**(C1 的 whitewash 面 + I1 的 self-bootstrap 面)。全 merge 前 whole-branch opus review + full suite。
