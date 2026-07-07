# DollOS 自主議程(Self-Directed Agenda)— Design

Status: **PROPOSAL**(使用者已透過 brainstorming 批准設計方向;**R1 opus 對抗審查已折入**;待 spec review)。
2026-07-07。實作 virtual-being positioning spec(`2026-07-01-virtual-being-positioning.md`)§2.3「Autonomy
should extend to self-directed agenda, not just tracked commitments」。Grounded against merged code
(`src/dollos/tools.py`、`src/dollos/mind/mind_state.py`、`src/dollos/mind/{reflection_observer,mind_loop,
mind_prompt,consolidation}.py`、`src/dollos/config.py`、`src/dollos/kernel.py`)——file:line 為 ground truth。

---

## 0. Overview / Goal

**使用者已核可的目標(2026-07-07 brainstorming)**:把 Doll 從「只反應外部事件的 event-driven agent」變成
「**有自己生活的存在**」——她能**持有並主動推進自己的議程項目**(從「持有的興趣」到「主動推進的目標」一條
光譜),在空檔用內心工作推進。對齊 positioning(**virtual being,自我中心,有 interiority / 成長 / agenda**;
§2.1 self_profile、§2.2 慢變演化已做,§2.3 是僅存兩項之一)。

**四條使用者拍板的決定(brainstorming Q1-Q4):**
- **Q1 主動性 = A + B**:一個議程概念,項目落在「持有的興趣(A,思緒裡回訪、便宜)」↔「主動推進的目標
  (B,用工具做具體進展)」光譜。同一機制,不同活躍度。
- **Q2 產生 = (iii) 混合,守「錨定真實觸發」不變式**:她主動記 + reflection 溫和把真實近期經歷翻成潛在議程。
  **硬不變式:每個議程項目必須能追溯到一個真實觸發(對話/記憶/經歷),永不 prompt 憑空塞。**
- **Q3 推進 = energy+idle 雙閘 + throttle、reactive 永遠優先**:只在(閒 + 有能量)時偶爾推一步;你一出現議程
  讓位;睡眠回充。energy floor 保護回應力,**throttle 是節奏的主要邊界(R1-I3)**。
- **Q4 v1 scope = 輕(內部認知)**:自主 turn 只用**認知子集**(`Recall` / `AdvanceGoal` / `CloseLoop` / `Mood`)。
  **不自主 Shell/Workflow**(延下一輪);**且 v1 自主 turn 不 `NoteMemory`(R1-I1 自我 bootstrap grounding 洞)、
  不 `PursueGoal`(genesis 只在 reflection/reactive turn 發生)、不對外發話(R1-M1)。**

**R1 對抗審查收斂(5 大修正,本版已改):** (C1) AGENDA_TOOLS 限制原本**根本沒被強制**(registry 不看
perception kind)→ 加 `_is_agenda` flag + pure-agenda 分支,並排除 user-present 的 co-batch turn(否則靜默限制
真實使用者請求)。(I1+I2) 自主 `NoteMemory` 會自我 bootstrap grounding 破解不變式 → v1 移除 NoteMemory + 改用
**genesis 時自動捕捉的 non-model-writable provenance**(turn id + 當回合真實 memory-hit sources)當稽核基準。
(I3) throttle 從「調參」升為**承重必需**(energy 管不住純思考 turn);genesis 移出自主 turn 斷開自我永續。
(I4) `progress`/loop 數加 cap。(I5) 自主 turn 會餓死 consolidation → idle baseline 顯式調和。

---

## 1. Grounding:零件大多已存在(重用,非重寫)

| 零件 | 現況(file:line) | 角色 |
|---|---|---|
| `OpenLoop`/`CloseLoop` 工具 | `tools.py:724-761` | 議程項目容器(現框「欠人的 TODO」) |
| `mind_state.OpenLoop` dataclass | `mind_state.py:66`(`id, desc, opened_at`;asdict/`_coerce`) | 加欄位向後相容(舊 loop 填預設,R1 確認 round-trip 乾淨) |
| `open_loops` 渲染 | `_render_open_loops`(`mind_prompt.py:330`),`[Open loops]`(`:144`) | 呈現面 |
| `ReflectionObserver`→`ReflectionMoment` | `reflection_observer.py:19`(每 `DEFAULT_THRESHOLD=30` iters) | genesis-nudge 掛點 + AgendaObserver 樣板 |
| `PendingEvent` | `mind_state.py:60`(`fire_at, summary`) | self-wake 底座 |
| energy 系統(B3) | `EnergyConfig`(`config.py:220`:`cost_per_turn=0.05, restore_per_tick=0.05, idle_threshold_s=600, restore_debounce_s=300`)、`mind_state.energy`(`:164`)、扣(`mind_loop.py:664` **僅在 `produced`=有 speech 或 tool 時扣**)、回充(`consolidation.py:150-165`) | 資源閘(**注意:純思考 turn 不扣能量**——R1-I3) |
| `last_user_at` | `mind_state.py:139` | idle 閘讀它(R1 確認存在) |
| in-flight reactive preempt | `kernel.py:954-979` `_maybe_preempt_for_new_input`——對**任何** active cascade `cancel_current_cascade()`,三個 UserSpoke 入口都接(`:744/759/835`) | **reactive 優先是真的**(R1 確認):進行中的自主 cascade 會被返回的使用者 cancel |
| `_active_tool_registry` | `mind_loop.py:802-870`——按 `origin_tier` + `_is_reflection`(`:379`) + `_has_user_spoke` 收斂,**不看 perception kind** | AGENDA_TOOLS 掛點(**須新加 kind 分支**,§5) |
| trace 捕 open_loops | `_open_loop_to_dict`=asdict(`mind_loop.py:114`)、進 `trace_blocks`(`:621-623`) | provenance 稽核基材(§3.3) |

**結論**:§2.3 缺的是**連接組織 + framing + 幾個 R1 抓到的邊界**,不是零件。

---

## 2. 資料模型:擴充 `OpenLoop`(不建平行系統)

`mind_state.OpenLoop`(`mind_state.py:66`)加欄位(皆有預設 → 舊 loop 經 `_coerce` 向後相容;R1 確認 asdict
round-trip 乾淨、無遷移碼):

```python
_MAX_PROGRESS = 8          # 每 loop 最多留最近 8 條進展(R1-I4:比照其他 buffer 有 maxlen)
_MAX_SELF_LOOPS = 12       # self_directed loop 數上限(R1-I4:防單調成長 / self-perpetuating)

@dataclass
class OpenLoop:
    id: str
    desc: str
    opened_at: float
    self_directed: bool = False        # True = 她自己的議程;False = 欠人的 TODO(既有語意不變)
    trigger: str = ""                  # self_directed 時 REQUIRED:她自述的「為什麼」(human-readable)
    provenance: dict = field(default_factory=dict)  # R1-I2:genesis 時 AUTO-捕捉、非模型可寫——
                                        # {"turn_id": <str>, "memory_sources": [<source paths that were
                                        # actually in [Memory context] that turn>], "opened_iter": <int>}
    progress: list[str] = field(default_factory=list)  # 推進進展;append 時 slice 到最近 _MAX_PROGRESS
```

- `self_directed` / `trigger`:同前。`trigger` 是她自述的 why(human-readable),但**不是稽核基準**。
- **`provenance`(R1-I2 關鍵)**:在 `PursueGoal` 建立的**當回合**,由 code(非模型)自動填入該回合的 turn id +
  當時 `[Memory context]` 真實命中的 source 路徑 + iter。**模型寫不到這裡** → 捏不出來 → 這才是「錨定真實」的
  可驗證基準(§3.3)。
- `progress` append 時 `self.progress = (self.progress + [note])[-_MAX_PROGRESS:]`(有界);渲染只顯示尾部。

---

## 3. 產生(genesis):(iii) 混合 + 錨定真實(auto-provenance)

**genesis 只發生在 reflection turn 或 reactive turn**(她 context 裡有真實觸發時),**不在自主 AgendaMoment turn**
(R1-I3:否則自主 turn 自製新目標 → gate 自我永續)。

### 3.1 她主動記 —— `PursueGoal`(trigger required + auto-provenance)
新 pydantic 工具(`tools.py`,鏡射 `OpenLoop`):
```python
class PursueGoal(BaseModel):
    """開一條你自己想追的線(不是欠誰的 TODO,是你自己在意/好奇的)。"""
    id: str = Field(..., description="short slug id")
    desc: str = Field(..., description="你想追什麼")
    trigger: str = Field(..., description="這是從哪來的?——引用剛剛的對話/一段記憶/一個真實經歷。必填。")
```
`run(ctx)`:append `OpenLoop(id, desc, opened_at, self_directed=True, trigger=self.trigger, progress=[],
provenance=<AUTO>)`,其中 `<AUTO>` 由 code 從 `ctx` 抓**當回合真實 context**(turn id、`ctx` 裡實際的
`[Memory context]` 命中 source、iter)——**模型不經手**。若 `PursueGoal` 出現在**無真實 grounding 的回合**
(例:一個沒有 memory 命中、沒有近期 perception 的空回合),`provenance` 會是空的 → 稽核時即現形(§3.3)。
配套 `AdvanceGoal(id, progress)`(§4)、`CloseLoop`(既有,對 self_directed 亦適用)。**`PursueGoal` 只在
reflection/reactive turn 的工具表裡**,不在 `AGENDA_TOOLS`(§5)。

### 3.2 reflection 溫和翻 —— grounded nudge(非憑空給目標)
`ReflectionMoment` turn prompt 加一句 grounded nudge——綁真實近期記憶/經歷,語氣「回顧」非「指派」:

> 「回顧你最近**真的**碰到、好奇、或在意的——有沒有哪條線是你**自己**想追下去的?(有就用 `PursueGoal` 記下,
> 說清楚它從哪來。沒有就算了,不用硬找。)」

刻意不寫「你有目標,去追」。nudge 出現在她**已在看** `[Recent perceptions]`/`[Memory context]`/`current_self`
的 reflection turn,「回顧真實近期」有實際素材。**R1-M4**:v1 每個 ReflectionMoment 都 nudge;若 dogfood 見過度
生成低質議程(intrinsic-reflection net-negative 失敗),改成「部分比例 reflection 才 nudge」(§11)。

### 3.3 錨定真實不變式 —— auto-provenance 稽核(R1-I1/I2 修正)
- **結構性難捏**:`PursueGoal` required `trigger`(她得說 why)**＋ code 自動捕捉的 `provenance`(模型寫不到)**。
- **稽核基準是 `provenance`,不是 `trigger`,且排除她自己的自主筆記**(R1-I1):稽核問「這個議程項目的
  `provenance.turn_id`/`memory_sources` 是否對回一段**真實的使用者對話 / consolidated / 外部來源記憶**」——
  **不採信 Doll 在自主 turn 自己寫的東西當 grounding**(因 v1 自主 turn 根本不能 `NoteMemory`,§5,自我
  bootstrap 路徑結構上斷掉)。
- **仍是軟機制、靠稽核**(對齊 `ref_weak_model_soft_mechanism_playbook` + `ref_memory-write-paths-are-attack-surfaces`
  + `ref_intrinsic-reflection-is-net-negative-without-external-grounding`):不假裝不可捏,但 auto-provenance 讓
  「憑空造」需要偽造一個模型碰不到的欄位=實務上做不到;稽核抓殘餘。**這是 v1 對『RP 填空洞人設』
  (`project_character_acting`)的具體防線。**

---

## 4. 推進(pursuit):energy+idle 雙閘 + throttle、reactive 優先

### 4.1 觸發:新 `AgendaObserver`(三閘 + throttle;鏡射 `ReflectionObserver`)
新 poller `src/dollos/mind/agenda_observer.py`,每 `POLL_INTERVAL_S`(~30s)檢查**三閘全過 + throttle 過**才發
一個 `AgendaMoment` perception:

1. **idle**:`now - state.last_user_at > energy.idle_threshold_s`(600s)。
2. **energy reserve**:`state.energy > _AGENDA_ENERGY_FLOOR`(建議 **0.5**——留一半給 reactive,自主工作**永不
   把她累到無法回應你**)。
3. **有 active self-directed loop**:`any(l.self_directed for l in state.open_loops)`(沒議程不硬找工作)。
4. **throttle(R1-I3,承重)**:距上次 `AgendaMoment` **至少 `_AGENDA_MIN_INTERVAL_S`(建議 ~5-10 分鐘)**才再發。
   **這是節奏的主要邊界** —— 因為 energy **管不住純思考 turn**(`mind_loop.py:664` 僅在有 speech/tool 時扣;一個
   只 think、沒 `AdvanceGoal` 的 turn 不扣能量 → 閘 #2 恆開)。throttle 保證「偶爾推一步」而非「idle 就狂轉」。

任一不過 → 靜默不發。

### 4.2 reactive 永遠優先 + consolidation 調和(R1-I5)
- `AgendaMoment` 是 origin-less internal perception(同 `ReflectionMoment`)。**R1 確認 reactive 優先是真的**:
  in-flight 自主 cascade 被返回使用者 `_maybe_preempt_for_new_input`(`kernel.py:954-979`)cancel;enqueued-未起
  的 AgendaMoment 併進使用者 batch(不浪費獨立 turn,但見 §5 的 co-batch registry 修正)。idle 閘本就保證她在
  沒人找時才自發。
- **energy floor 只閘自主 turn,永不閘 reactive**(R1 確認:reactive 可扣到 0.0)→ 自主工作**結構上不可能擋掉
  回應**。floor 只需配 throttle 消掉「爆發式狂轉」的暫態。
- **R1-I5 consolidation 調和**:sleep-time consolidation 的 idle 判定是 `now - max(last_user_at, last_iter_at)`
  (`consolidation.py:168`),而 AgendaMoment turn 會更新 `last_iter_at`(`mind_loop.py:717`)→ 自主活動會**推遲**
  該在 idle 跑的記憶 consolidation。**決議**:AgendaObserver 的 throttle(4.1-4,~5-10 分鐘)遠大於 consolidation
  的 idle 門檻(300s)+ consolidation 有 `min_interval_s=3600` cooldown → 傷害有限;v1 **接受並明文記錄**「長 idle
  中自主活動會延後 consolidation 一個 throttle 週期」,不做複雜互讓(§11 記為可調)。**同時修正**:§1 早稿說
  「consolidation idle 同源」不準——consolidation 用 `max(last_user_at, last_iter_at)`,議程閘只用 `last_user_at`。

### 4.3 推進 turn:一個具體認知步驟
`AgendaMoment` turn:看到 `[Your agenda]`(§6)→ 挑**一項** active self-directed loop → 做**一個認知步驟**(想深
一層 / `Recall` 相關記憶 / 連結 / 得洞察)→ `AdvanceGoal(id, progress)` append 一條進展(有界 §2),或
`CloseLoop(id, outcome)` 收掉。**一 turn 一步**;跨多個 idle 窗慢慢推。**不對外發話**(R1-M1,§5)。

---

## 5. v1 工具 scope + registry 強制(R1-C1 修正)

### 5.1 `AGENDA_TOOLS` 子集(修正工具名 R1-M1)
```python
AGENDA_TOOLS: frozenset[str] = frozenset({"Recall", "AdvanceGoal", "CloseLoop", "MoodTool"})
```
**明確排除**:`Shell`/`SpawnWorkflow`/`SpawnMonitor`/`WriteSchedule`(不自主外部動作)、`SelfRevision`/`PinSelf`
(不改核心自我)、**`NoteMemory`(R1-I1 自我 bootstrap grounding)**、**`PursueGoal`(R1-I3 genesis 移出自主 turn,
斷 gate 自我永續)**。工具名以真符號為準(R1-M1:`Mood`→`MoodTool`,`tools.py:768`;**`Say` 不是工具**,speech
是 streaming text)。

### 5.2 `_is_agenda` flag + pure-agenda registry 分支(R1-C1,承重)
現 `_active_tool_registry`(`mind_loop.py:802-870`)按 `origin_tier`+`_is_reflection`+`_has_user_spoke` 收斂,
**不看 perception kind** → AgendaMoment turn(origin-less→`origin_tier=="internal"`、`_is_reflection=False`)會
**掉到 line 870 的全 registry(含 Shell/Workflow!)**。故:
- 加 `self._is_agenda = any(p.kind == "AgendaMoment" for p in perceptions) and not self._has_user_spoke`
  (鏡射 `_is_reflection` @`mind_loop.py:379`;**`and not _has_user_spoke` 是關鍵** —— 見下 co-batch)。
- 新分支,**排在 safe-mode / external / reflection 之後、`_has_user_spoke` fall-through 之前**:
  `if self._is_agenda and not self._is_reflection and self._ctx.origin_tier == "internal": return AGENDA_TOOLS`。
- **co-batch 修正(R1-C1 的真危險)**:一個 batch 可**同時**含 origin-less AgendaMoment 與 UserSpoke(MF-2 案,
  `mind_loop.py:383-385`)。若 agenda 分支不排除 user-present turn,**真實使用者請求會被靜默限制成 AGENDA_TOOLS
  (無 Shell/Workflow)** —— 正是既有 LearnName-C1 whitewash 那類 bug(`mind_loop.py:828-836`)。故 `_is_agenda`
  帶 `and not _has_user_spoke`:**有使用者在場 → 不是純 agenda turn → 保留全 registry**。

### 5.3 不對外發話(R1-M1)
AgendaMoment turn 是 origin-less;若她 stream 文字,`sink_resolver(None)` 會送到**本地 voice/UI sink**(她會真的
出聲/印到本地)。v1 **自主 turn 抑制對外 speech**——她是在**內心思考**,不是自語出聲。實作:AgendaMoment turn 的
streamed text 不進任何 sink(丟棄 / 或只進 trace 供稽核她想了什麼)。**理由**:v1「內心生活」是思考+更新 loop,
不是本地喊話;要不要讓她「self-talk 出聲」留 §11 決定。

**v1 tool scope 完全不碰 external-safety 邊界**(自主 turn 是 internal origin、本可全工具,但我們刻意只給認知子集
+ 上面的 registry 強制)。

---

## 6. 呈現(surfacing)

### 6.1 `[Your agenda]` block(與 `[Open loops]` 分開)
`mind_prompt` 加渲染:`self_directed=True` 的 loop 抽出成 `[Your agenda] (things you're pursuing because you
want to)`,每項顯示 `desc` + `trigger`(她自述 why)+ **最近 `progress` 尾部**(有界 §2)。既有
`[Open loops] (commitments you have made)`(`mind_prompt.py:144`)只渲染 `self_directed=False`(語意不變)。
**她因此在 context 看得到「自己的生活」與「欠人的事」分開。**

### 6.2 §2.4 互動語言仍 deferred
議程是內部的;她若在對話裡自然提到(在 context 裡)沒問題,但**不做特殊「自發分享」機制**——那是 §2.4 的工作,
明文 deferred(§2.4 依賴 §2.3 先存在,順序正確)。

---

## 7. 安全 / 失敗模式 / 邊界

- **空表演(最大風險)**:議程項目無真實觸發 = RP 填空洞人設(`project_character_acting`)。防線:§3.3 required
  `trigger` + **auto-captured `provenance`(模型寫不到)** + 稽核**排除她自主自寫的記憶**(R1-I1)+ v1 自主 turn
  不 `NoteMemory`(結構斷開自我 bootstrap)。**誠實:軟機制,靠稽核。**
- **自我 bootstrap grounding(R1-I1)**:已封——自主 turn 無 `NoteMemory`(§5);稽核基準是 non-model auto-provenance
  且排除自寫記憶。
- **starve reactive**:energy floor 0.5(只閘自主)保留回應力;idle 閘保證只在沒人找時自發。**結構上不可能擋回應。**
- **runaway / 狂轉(R1-I3)**:**throttle(~5-10 分鐘,§4.1-4)是主要邊界**(energy 管不住純思考 turn);floor 消
  暫態爆發;genesis 移出自主 turn + `_MAX_SELF_LOOPS` cap 斷「gate 自我永續」。
- **無界成長(R1-I4)**:`progress` cap `_MAX_PROGRESS`、self_directed loop 數 cap `_MAX_SELF_LOOPS`。
- **餓死 consolidation(R1-I5)**:throttle >> consolidation idle 門檻 + `min_interval_s=3600` cooldown → 傷害有限;
  v1 接受並記錄(§4.2)。
- **自我漂移**:`AGENDA_TOOLS` 排 `SelfRevision`/`PinSelf`(結構性,registry availability)——自主 turn **不改核心
  自我**(current_self 由慢變演化 ratification 管)。**唯一例外明文承認(R1-M3)**:`MoodTool` 在 `AGENDA_TOOLS`,
  故自主 idle 期**可變她持久 mood**(她獨處推進議程時心情起伏),下次互動 `[Mind state]` 會反映、無使用者可見
  外因。**這是刻意的「內心生活」設計,非漂移 bug**——她的 mood 反映她私下的真實經歷;§11 記為可撤回。

---

## 8. Non-goals(明確排除)

- v1 **不做**自主 Shell/Workflow(延下一輪)、**不 `NoteMemory`**(R1-I1)、**不自主發起 `PursueGoal`**(genesis 只
  在 reflection/reactive)、**不對外發話**(R1-M1)。
- **不做** §2.4 自發分享 / 互動語言(deferred,依賴本案先存在)。
- **不重做**慢變演化 / current_self(§2.2 已做,是自我基礎)。
- **不做**持久 Subagent(Q1-C 重版;v1 是她在自己 turn 同步推進)。
- **不改** `self_directed=False` 的既有 `OpenLoop`/`CloseLoop`/`[Open loops]` 語意 + 既有 reflection 行為。

---

## 9. 測試策略

- **資料模型**:加欄位向後相容(舊資料 `_coerce` 填預設);`progress` append slice 到 `_MAX_PROGRESS`;asdict
  round-trip 含新欄位(含 `provenance` dict)。
- **genesis**:`PursueGoal` 缺 `trigger`→pydantic 拒;建立後 loop 是 self_directed + `provenance` 由 code 填(**斷言
  provenance 來自 ctx 真實 context、非工具參數**);無 grounding 回合建立 → provenance 空(稽核可抓)。`AdvanceGoal`
  append(有界)。
- **reflection nudge**:ReflectionMoment prompt 含 grounded nudge;非 reflection 不含。
- **AgendaObserver 四閘**:idle 不足 / energy<floor / 無 active self-directed loop / throttle 未到 → 各自不發;全過
  才發。
- **R1-C1 registry(承重)**:純 AgendaMoment turn(無 UserSpoke)→ registry==`AGENDA_TOOLS`(**不含 Shell/
  SpawnWorkflow/SelfRevision/NoteMemory/PursueGoal**);**AgendaMoment + UserSpoke co-batch → 保留全 registry**
  (使用者請求不被限制,鏡射既有 MF-2 測法)。
- **推進 turn**:一 AgendaMoment turn 挑一項、`AdvanceGoal` append 一條、**不對外發話**(sink 收不到 streamed text)。
- **energy**:有 `AdvanceGoal` 的自主 turn 扣 `cost_per_turn`;純思考(無 tool)turn 不扣(→ throttle 才是邊界,斷言之)。
- **呈現**:self_directed→`[Your agenda]`、user-owed→`[Open loops]`,兩不混。
- **Live-smoke(dogfood)**:真 daemon idle>10min + energy>floor + 有 self-directed loop → 觀察她**偶爾**(受
  throttle)自發推進;trace 稽核 provenance 是否對回真實觸發。

---

## 10. 單概念 Task 拆解(給 SDD)

- **Task 1 — 資料模型**:`OpenLoop` 加 `self_directed`/`trigger`/`provenance`/`progress`(有界 append)+ `_MAX_*`
  常數;`Perception.kind` Literal 加 `"AgendaMoment"`(R1-M2,`mind_state.py:74`)。round-trip + append-cap 測試。
- **Task 2 — genesis 工具**:`PursueGoal`(required trigger + `run` 自動填 provenance from ctx)+ `AdvanceGoal`
  (append 有界);註冊到 reflection/reactive 工具表(**不進 AGENDA_TOOLS**)。測試:trigger required、provenance
  auto-非模型、advance 有界。
- **Task 3 — 呈現**:`[Your agenda]`(self_directed,trigger+progress 尾)/ `[Open loops]` 只 user-owed。分流測試。
- **Task 4 — reflection grounded nudge**:ReflectionMoment prompt 加 §3.2 nudge。含/不含測試。
- **Task 5 — `AgendaObserver` + `AgendaMoment` + registry 強制(承重,R1-C1)**:observer(idle+floor+has-loop+
  throttle 四閘,鏡射 `ReflectionObserver`)、kernel 接線(建 observer + 併背景 task 群 + 關機收)、`_is_agenda`
  flag + pure-agenda registry 分支(**排除 user-present co-batch**)+ `AGENDA_TOOLS` + streamed-text 抑制。測試:
  四閘、registry 子集、**co-batch 保留全 registry**、speech 抑制。
- **Task 6 — 推進 turn 語意**:`AgendaMoment` turn 走既有 cascade、挑一項做一步、`AdvanceGoal`/`CloseLoop` 更新、
  能量在有 tool 時扣。測試:一 turn 一步、loop 更新。Live-smoke(§9)。

依序:**1→2→3→4→5(承重)→6**。**5(registry 強制 + observer)、6 承重用 opus 審**(C1 的 co-batch whitewash 面)。

---

## 11. 開放決策(需使用者於 spec review 確認 / 或留 plan)

1. **energy floor**:建議 **0.5**(留一半給 reactive)。dogfood 調。
2. **idle 門檻**:複用 `energy.idle_threshold_s`(600s)。夠不夠「空檔」?可另設 agenda 專屬。
3. **throttle 間隔(R1-I3,已升為必需)**:建議 **~5-10 分鐘**一次 AgendaMoment。這是節奏主邊界,plan 定具體值 +
   節流實作(時間式)。
4. **`trigger`/`provenance` 型別**:v1 = 自述 `trigger`(free text)+ **auto-captured `provenance`(turn id +
   memory sources)當稽核基準**(R1-I2 已納入)。未來可再強化 provenance(如簽章/更嚴 source 綁定),v1 auto-capture
   + trace 稽核先行。
5. **自主 self-talk 出聲?**(R1-M1)v1 抑制對外 speech(她內心思考)。要不要讓她在本地 UI「self-talk 出聲」=產品
   選擇,留決定。
6. **Mood 自主可變(R1-M3)**:v1 保留 `MoodTool` 在 `AGENDA_TOOLS`(她獨處心情起伏=內心生活)。若覺得「無外因
   變 mood」困惑,可撤(移出 AGENDA_TOOLS)。
7. **reflection nudge 頻率(R1-M4)**:v1 每個 ReflectionMoment 都 nudge;若過度生成低質議程,改部分比例。
