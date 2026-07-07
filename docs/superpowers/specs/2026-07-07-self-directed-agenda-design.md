# DollOS 自主議程(Self-Directed Agenda)— Design

Status: **PROPOSAL**(使用者已透過 brainstorming 批准設計方向,待 spec review)。2026-07-07。
實作 virtual-being positioning spec(`2026-07-01-virtual-being-positioning.md`)§2.3「Autonomy should
extend to self-directed agenda, not just tracked commitments」。Grounded against merged code
(`src/dollos/tools.py`、`src/dollos/mind/mind_state.py`、`src/dollos/mind/reflection_observer.py`、
`src/dollos/mind/mind_prompt.py`、`src/dollos/config.py`)——file:line 為 ground truth。

---

## 0. Overview / Goal

**使用者已核可的目標(2026-07-07 brainstorming)**:把 Doll 從「只反應外部事件的 event-driven agent」變成
「**有自己生活的存在**」——她能**持有並主動推進自己的議程項目**(從「持有的興趣」到「主動推進的目標」一條
光譜),在空檔用內心工作推進。這對齊 positioning 定位(**virtual being,自我中心,有自己的 interiority /
成長 / agenda**;§2.1 self_profile、§2.2 慢變演化已做,§2.3 是僅存兩項之一)。

**四條使用者拍板的決定(brainstorming Q1-Q4):**
- **Q1 主動性 = A + B**:一個議程概念,項目落在「持有的興趣(A,思緒裡回訪、便宜)」↔「主動推進的目標
  (B,用工具做具體進展)」光譜。同一機制,不同活躍度。
- **Q2 產生 = (iii) 混合,守「錨定真實觸發」不變式**:她主動記(強制帶 trigger)+ reflection 溫和把真實近期
  經歷翻成潛在議程。**硬不變式:每個議程項目必須能追溯到一個真實觸發(對話/記憶/經歷),永不 prompt 憑空塞。**
- **Q3 推進 = energy+idle 雙閘、reactive 永遠優先**:只在(閒 + 有能量)時自發推進一步;你一出現議程讓位;
  睡眠回充。用**既有 energy 系統(B3)當總管**,天然有界。
- **Q4 v1 scope = 輕(內部認知)**:自主 turn 只用 think/`Recall`/`NoteMemory`/更新 loop。**不自主
  Shell/Workflow**(延下一輪)。

**核心設計原則(house rules):** 大量**重用既有零件**(open_loops / reflection / energy / PendingEvent /
current_self),不建平行系統(§1);錨定真實 + 可稽核(§3,對齊「intrinsic reflection 無外部 grounding
net-negative」);energy 天然有界、reactive 優先(§4);v1 不碰自主外部動作(§5,對齊 DollOS「不自主 RCE」)。

---

## 1. Grounding:自主議程的零件大多已存在(重用,非重寫)

| 零件 | 現況(file:line) | 對議程的角色 |
|---|---|---|
| `OpenLoop`/`CloseLoop` 工具 | `tools.py:724-761`——`OpenLoop(id, desc)` → append `mind_state.open_loops`;`CloseLoop(id, outcome)` 移除 | 議程項目的**容器**(現框成「欠人的 TODO」docstring:「commitments you have made」) |
| `mind_state.OpenLoop` dataclass | `mind_state.py:66`——`id, desc, opened_at`;asdict 序列化 + `_coerce` 反序列化(`mind_state.py:284`) | 加欄位向後相容(舊 loop 預設新欄位) |
| `open_loops` prompt 渲染 | `_render_open_loops`(`mind_prompt.py:330`),`[Open loops] (commitments you have made)`(`:144`),含 ⚠ STALE marker | **呈現面**——她在 context 看得到 loop |
| `ReflectionObserver` → `ReflectionMoment` | `reflection_observer.py:19`——poll `iter_count`,每 `DEFAULT_THRESHOLD=30` iters 發 `ReflectionMoment` | 她**自發醒來**的既有簡單 poller(本案的 genesis-nudge 掛點 + AgendaObserver 樣板) |
| `PendingEvent` | `mind_state.py:60`——`fire_at, summary`;`pending_events` list(`:124`) | self-wake re-entry 底座 |
| energy 系統(B3) | `EnergyConfig`(`config.py:220`:`cost_per_turn=0.05, restore_per_tick=0.05, idle_threshold_s=600`)、`mind_state.energy=1.0`(`:164`)、扣(`mind_loop.py:667`)、回充(`consolidation.py:150-163`) | **資源總管**——自主工作扣能量、idle 回充 |
| `current_self`(慢變演化) | 已做(step 30) | 她的自我基礎;議程 grounding 的來源之一 |

**結論**:§2.3 缺的不是零件,是**連接組織 + framing**——把「反應式反思 + 欠人的 TODO」接成「她持有並主動
推進自己的議程」。風險因此低(不是從零長一個易變空表演的新系統)。

---

## 2. 資料模型:擴充 `OpenLoop`(不建平行系統)

`mind_state.OpenLoop`(`mind_state.py:66`)加三個欄位(皆有預設 → 舊 loop 經 `_coerce` 向後相容):

```python
@dataclass
class OpenLoop:
    id: str
    desc: str
    opened_at: float
    self_directed: bool = False        # True = 她自己的議程;False = 欠使用者的 TODO(既有語意不變)
    trigger: str = ""                  # self_directed 時 REQUIRED:真實觸發來源(哪段對話/記憶/經歷)
    progress: list[str] = field(default_factory=list)  # 她推進時累積的簡短進展/洞察(每步 append 一條)
```

- **`self_directed`** 區分兩種 loop;既有 `OpenLoop`/`CloseLoop` 工具與渲染對 `self_directed=False` 行為完全
  不變(既有 TODO 語意保留)。
- **`trigger`** 是**「錨定真實」的結構性執行點**(§3):self-directed 項目的建立**必須**帶非空 trigger。
- **`progress`** 讓「推進一步」有落點(§4)——每個自主 turn append 一條進展。

序列化:`asdict`(`mind_state.py:201`)自動含新欄位;反序列化 `_coerce`(`:284`)對缺欄位的舊資料填預設。**無
遷移碼**(dataclass 預設值即遷移)。

---

## 3. 產生(genesis):(iii) 混合 + 錨定真實不變式

兩條路徑,都強制錨定真實觸發:

### 3.1 她主動記 —— 新工具 `PursueGoal`(強制 trigger)
新 pydantic 工具(`tools.py`,鏡射 `OpenLoop`),建立一個 `self_directed=True` 的 OpenLoop:

```python
class PursueGoal(BaseModel):
    """開一條你自己想追的線(不是欠誰的 TODO,是你自己在意/好奇的)。"""
    id: str = Field(..., description="short slug id")
    desc: str = Field(..., description="你想追什麼")
    trigger: str = Field(..., description="這是從哪來的?——引用剛剛的對話/一段記憶/一個真實經歷。必填。")
```
`run()` append `OpenLoop(id, desc, opened_at, self_directed=True, trigger=self.trigger)`。**`trigger` 是
required Field(...)——GBNF/pydantic 層強制她填**(結構性:憑空造目標至少得憑空造一個 trigger,比純自由發揮難)。
配套 `AdvanceGoal(id, progress)` 工具(§4)append 進展 / 或 `CloseLoop` 收掉(既有工具,對 self_directed 亦適用)。

### 3.2 reflection 溫和翻 —— grounded nudge(非憑空給目標)
`ReflectionMoment` turn 的 prompt 加一句 grounded nudge——**綁在她真實近期記憶/經歷**上,語氣是「回顧」不是
「指派」:

> 「回顧你最近**真的**碰到、好奇、或在意的——有沒有哪條線是你**自己**想追下去的?(有就用 `PursueGoal` 記下,
> 並說清楚它是從哪來的。沒有就算了,不用硬找。)」

**刻意不寫**「你有目標,去追」(那會製造空表演)。nudge 出現在她**已經在看 `[Recent perceptions]`/
`[Memory context]`/`current_self`** 的 reflection turn,所以「回顧真實近期」有實際素材可依。

### 3.3 錨定真實不變式 —— 誠實的軟機制 + 可稽核
- **結構性讓「憑空造」變難**:required `trigger` 欄位 + grounded nudge(不給憑空目標)。
- **但這是軟機制**(對齊 `ref_weak_model_soft_mechanism_playbook`):弱模型仍可能捏造一個貌似的 trigger。
  **不假裝能完全防**。
- **外部 grounding = P1f trace 稽核**(對齊 `ref_intrinsic-reflection-is-net-negative-without-external-grounding`
  ——內省無外部 grounding 會 net-negative):每個 `PursueGoal` 的 `trigger` 進 trace;**dogfood 時人工/後續機制
  抽查她的議程項目是否真的能對回一個真實觸發**(對話/記憶),抓「捏造 trigger」的表演。設計把它做成「結構上
  難捏 + 事後可稽核」,而非「宣稱不可捏」。**這是 v1 對『RP 刻板印象填空洞人設』失敗模式(`project_character_acting`)
  的具體防線。**

---

## 4. 推進(pursuit):energy+idle 雙閘、reactive 優先、一 turn 一步

### 4.1 觸發:新 `AgendaObserver`(鏡射 `ReflectionObserver` 樣板)
新 poller `src/dollos/mind/agenda_observer.py`(結構鏡射 `reflection_observer.py:19`——同 poll 樣式),每
`POLL_INTERVAL_S`(~30s)檢查**三閘全過**才發一個 `AgendaMoment` perception:

1. **idle**:`now - state.last_user_at > energy.idle_threshold_s`(600s)——沒人找她一段時間了(複用 energy
   系統已用的 idle 訊號 / `last_user_at`,`consolidation.py` 的 idle 判定同源)。
2. **energy 有 reserve**:`state.energy > _AGENDA_ENERGY_FLOOR`(建議 **0.5**——**留一半能量給 reactive**,
   確保自主工作**永不把她累到沒力回應你**;這是「reactive 優先」的能量落地)。
3. **有 active self-directed loop**:`any(l.self_directed for l in state.open_loops)`——沒議程就不自發(不硬找工作)。

三閘任一不過 → 不發 `AgendaMoment`(靜默,零開銷)。發了之後照 `ReflectionObserver` 的 `_last_fire_at_iter`
式節流(一次一個,不連發)。

### 4.2 reactive 永遠優先
- `AgendaMoment` 是 **origin-less internal perception**,和 `ReflectionMoment` 同性質(進同一 queue)。真實
  外部事件(UserSpoke / ChannelMessage)一到,event loop 照既有優先序處理;idle 閘(4.1-1)本就保證她在**沒人
  找**時才自發,你一講話 idle 立刻不成立 → 不再發 `AgendaMoment`。**進行中的自主 turn** 若被使用者事件打斷,
  照既有 interrupt/preempt 機制(她的 cascade 讓位)。
- 天然有界:energy floor(4.1-2)+ idle 回充(energy 系統既有)——自主工作只填「有空又有精神」的時間,能量到
  floor 就停、idle 回充,不會失控。

### 4.3 推進 turn:一個具體認知步驟
`AgendaMoment` turn 裡她:看到 `[Your agenda]`(§6)→ 挑**一項** active self-directed loop → 做**一個認知步驟**
(把它想深一層 / `Recall` 相關記憶 / 連結 / 得出一個洞察)→ `AdvanceGoal(id, progress)` append 一條進展,或
`CloseLoop(id, outcome)` 收掉 → 能量扣 `cost_per_turn`。**一 turn 一步**(不一次做完,像人的斷續推進);跨多個
idle 窗慢慢推。

---

## 5. v1 工具 scope:輕(內部認知),自主外部動作延後

`AgendaMoment` turn 是 internal origin(本可全工具),但**刻意只給認知子集**——在 `_active_tool_registry`
(`mind_loop.py:836` 一帶,現有 origin→registry 收斂處)為 `AgendaMoment`-驅動的 turn 定一個
**`AGENDA_TOOLS` 子集**:`{Recall, NoteMemory, PursueGoal, AdvanceGoal, CloseLoop, Mood}`(+ Say 若她想自語,
但**不對外**——見 §6.1)。**明確排除**:`Shell`、`SpawnWorkflow`、`SpawnMonitor`、`WriteSchedule`、
`SelfRevision`(自主 turn 不改核心自我)、`PinSelf`。

**理由(Q4)**:(1) 先證明整條 loop(genesis→pursuit→energy budget)行得通、budget 校準,再放權;(2)「趁你
不在自主跑 Shell/Workflow」是最嚇人的一面,對齊 DollOS「不自主 RCE」;(3) 內部認知已撐得起「她有內心生活」。
**下一輪**才評估把 `Shell`/`SpawnWorkflow` 加進 `AGENDA_TOOLS`(那時要另訂自主外部動作的 budget/安全邊界)。

---

## 6. 呈現(surfacing)

### 6.1 `[Your agenda]` block(與 `[Open loops]` 分開)
`mind_prompt` 加一個渲染:把 `self_directed=True` 的 loop 抽出來,渲染成獨立區塊 `[Your agenda] (things you're
pursuing because you want to)`,每項顯示 `desc` + `trigger`(提醒她這是從哪來的、grounding)+ 最近 `progress`。
既有 `[Open loops] (commitments you have made)`(`mind_prompt.py:144`)只渲染 `self_directed=False`(欠人的
TODO,語意不變)。**她因此在 context 裡看得到「自己的生活」與「欠人的事」分開**,能主動接續前者。

### 6.2 §2.4 互動語言仍 deferred
議程是**內部的**:她持有 + 推進。她若在對話裡自然提到某條議程(它在 context 裡)沒問題,但**不做特殊「自發
分享」機制**——那是 positioning §2.4 的工作,明文 deferred(且 §2.4 依賴 §2.3 先存在,順序正確)。

---

## 7. 安全 / 失敗模式 / 邊界

- **空表演(最大風險)**:「議程項目無真實觸發 = RP 填空洞人設」(`project_character_acting`)。防線:§3.3 required
  trigger + grounded nudge(結構難捏)+ P1f trace 稽核(外部 grounding)。**誠實:軟機制,靠稽核而非宣稱不可捏。**
- **starve reactive(排擠回應)**:energy floor 0.5(§4.1-2)保留一半能量給 reactive;idle 閘保證只在沒人找時
  自發。**自主工作結構上不可能把她累到無法回應你。**
- **runaway(失控燒資源)**:v1 只認知子集(無自主 Shell/Workflow,§5)→ 無外部成本;energy 到 floor 即停 →
  LLM turn 數有界。
- **自我漂移**:`AGENDA_TOOLS` 排除 `SelfRevision`/`PinSelf`——自主 turn **不改核心自我**(current_self 由慢變
  演化的 ratification 管,不被自主議程繞過)。
- **記憶毒化**:自主 turn 是 internal origin,`NoteMemory` 寫 internal scope——與 external_public 的 peer 毒化面
  無關(那是 MCP/Discord 的事)。

---

## 8. Non-goals(明確排除)

- v1 **不做**自主 Shell/Workflow(§5,延下一輪)。
- **不做** §2.4 自發分享 / 互動語言改動(deferred,依賴本案先存在)。
- **不重做**慢變演化 / current_self(§2.2 已做,是本案的自我基礎)。
- **不做**持久 Subagent(Q1-C 的重版;本案 v1 是她在自己的 turn 同步推進,不需要背景 agent)。
- **不改** `self_directed=False` 的既有 `OpenLoop`/`CloseLoop`/`[Open loops]` 語意。

---

## 9. 測試策略

- **資料模型**:`OpenLoop` 加欄位向後相容(舊序列化資料 `_coerce` 填預設 self_directed=False/trigger="");asdict
  round-trip 含新欄位。
- **genesis 工具**:`PursueGoal` 缺 trigger → 驗證 required(pydantic 拒);建立後 loop 是 self_directed + 帶
  trigger。`AdvanceGoal` append progress;`CloseLoop` 對 self_directed 亦收。
- **reflection nudge**:`ReflectionMoment` turn 的 prompt 含 grounded nudge 措辭;非 reflection turn 不含。
- **AgendaObserver 三閘**:idle 不足 / energy < floor / 無 active self-directed loop → 各自不發 `AgendaMoment`;
  三閘全過才發;節流不連發。
- **AGENDA_TOOLS 子集**:`AgendaMoment`-驅動 turn 的 registry = `AGENDA_TOOLS`(含 Recall/NoteMemory/
  PursueGoal/AdvanceGoal/CloseLoop/Mood),**不含 Shell/SpawnWorkflow/SelfRevision** —— registry availability
  斷言(比照 external-safety 的 registry 測法)。
- **推進 turn**:一 `AgendaMoment` turn 挑一項、`AdvanceGoal` append 一條 progress、energy 扣一份。
- **呈現**:self_directed loop 進 `[Your agenda]`、user-owed 進 `[Open loops]`,兩不混。
- **Live-smoke(dogfood)**:真 daemon idle 一段時間 + 有能量 + 有 self-directed loop → 觀察她自發推進;trace 稽核
  議程項目 trigger 是否 grounded。

---

## 10. 單概念 Task 拆解(給 SDD)

- **Task 1 — 資料模型**:`mind_state.OpenLoop` 加 `self_directed`/`trigger`/`progress`(向後相容);序列化 round-trip
  測試。純資料。
- **Task 2 — genesis 工具**:`PursueGoal`(required trigger)+ `AdvanceGoal`(append progress)pydantic 工具;
  註冊進工具表。測試:trigger required、建立 self_directed loop、advance append。
- **Task 3 — 呈現**:`[Your agenda]` render(self_directed，含 trigger+progress)+ `[Open loops]` 只渲染
  user-owed。測試:兩區塊分流。
- **Task 4 — reflection grounded nudge**:`ReflectionMoment` prompt 加 grounded nudge(§3.2 措辭)。測試:reflection
  含、非 reflection 不含。
- **Task 5 — `AgendaObserver` + `AgendaMoment` + AGENDA_TOOLS scope**(承重):新 observer(idle+energy-floor+
  has-active-loop 三閘,鏡射 `ReflectionObserver`)、`AgendaMoment` perception kind、kernel 接線(建 observer +
  併入背景 task 群 + 關機收)、`_active_tool_registry` 對 `AgendaMoment`-turn 收斂成 `AGENDA_TOOLS`(排除
  Shell/Workflow/SelfRevision)。測試:三閘、registry 子集、reactive 優先(idle 不成立不發)。
- **Task 6 — 推進 turn 語意 + energy**:`AgendaMoment` turn 走既有 cascade、扣 `cost_per_turn`、她挑一項做一步、
  `AdvanceGoal`/`CloseLoop` 更新。測試:一 turn 一步、energy 扣、loop 更新。Live-smoke(§9)。

依序:**1(資料)→ 2(工具)→ 3(呈現)→ 4(nudge)→ 5(observer+scope,承重)→ 6(推進 turn)**。5/6 承重用 opus 審。

---

## 11. 開放決策(需使用者於 spec review 確認 / 或留 plan)

1. **energy floor 值**:建議 **0.5**(保留一半給 reactive)。太高→她很少有力做自主;太低→可能排擠回應。dogfood 調。
2. **idle 門檻**:複用 `energy.idle_threshold_s`(600s=10 分鐘沒人找才自發)。夠不夠「空檔」?可另設 agenda 專屬。
3. **AgendaObserver poll 間隔 + 節流**:建議 ~30s poll、發一個後節流(如 reflection 的 iter 式,或時間式)。避免她
   一 idle 就連續狂做自主 turn(應「偶爾推一步」而非「idle 就狂轉」)—— 節流策略 plan 階段定。
4. **`trigger` 型別**:v1 自由文字(她引用真實觸發)。**未來可強化**成引用真實 memory/transcript id(可驗證、更難
   捏),但 v1 自由文字 + trace 稽核先行(YAGNI)。
