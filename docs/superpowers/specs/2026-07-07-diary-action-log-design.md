# DollOS 日記 = 看著當天 action log 寫的第一人稱反思 — Design

Status: **PROPOSAL**(brainstorming 批准;待 spec review)。2026-07-07。
virtual-being 定位收尾後的第一個實體 feature —— 內心生活的正確出口(§2.4 收斂時認定日記 > 對話中主動 surfacing,
因日記天生反表演:寫給自己非講給人聽、寫真做的事、留下=訓練資料)。Grounded against merged code。

---

## 0. Overview / Goal

**現況(對照 code 確認,更正先前誤述)**:DollOS **已經有第一人稱表達式日記** —— `WriteDiary` tool
(「first-person prose narrative reflecting on the day's events AND your emotional state」)+ kernel 每天 23:00
`_diary_scheduler` 提示她寫。它**不是**自動摘要。

**真正的兩個缺口(brainstorming 挖出)**:
1. **依據(拿什麼寫)** —— 她寫日記時手上只有 `recent_perceptions`(maxlen=20)的 recency 鑰匙孔 + mood,
   **不會讀當天完整記錄**。稍忙一點,早上的事早滑出窗了 → 日記被最近事主宰、或為填空而空泛(表演溫床,且侵蝕訓練資料價值)。
2. **時機(何時寫)** —— 被 kernel 寫死的 23:00 cron **命令**「請寫今天的日記(呼叫 WriteDiary)」寫的。

**目標**:讓日記變成使用者的心智模型 —— **她看著自己一天的 action log 寫的第一人稱反思**。

**使用者拍板(brainstorming)**:
- **DRY**:不新做 action-log 流;**把現有 transcript 長成完整 action log**(對話 + 有意義動作),日記讀它。
  recall/consolidation/演化順帶全部受惠(它們本來就讀 transcript)。
- **log set**:記外部動作(Shell/Workflow/Monitor + 結果)、自己/生活(PursueGoal/AdvanceGoal/CloseLoop/
  WriteSchedule/SelfRevision/PinSelf)、世界事件(Monitor 觸發、服務掛);**NoteMemory 只記輕指標**、
  **Mood 只記實質變化**;**跳過** Recall / Say(對話行已記)/ 內部喚醒觸發 / 讀狀態。
- **時機機制 1(v1,本 spec)**:定時提醒、**可 config 設定**、設一個**強制動作的 deadline** ——
  詮釋為「**專用日記回合,工具收窄成寫日記導向,不造假**」(下 §2.3)。
- **時機機制 2(延後)**:睡後 trigger(外部訊號提供),對齊 sleep-time-compute,需 OS suspend/resume plumbing,之後做。

---

## 1. Grounding(對照 code 的錨點)

| 零件 | file:line | 角色 |
|---|---|---|
| `append_transcript` | `memory_writer.py:22-47`(每回合一行 `- HH:MM:SS <主人說/我說>：…`,寫檔 + `index_file`) | **長成 action log 的地方**(擴一個 action 寫入路徑,同檔同格式) |
| tool 執行中央定點 | `_dispatch_tool`(`mind_loop.py:1394-1401`)—— `dispatch_one(name, args, ctx, registry)` → `r`,緊接 `record_tool_outcome` | **記「她的動作」的單一 hook**(拿得到 name/args/result) |
| 感知攝入迴圈 | `mind_loop.py:348-370`(逐 `p` 消化 perception,已對 UserSpoke/owner-ChannelMessage 寫對話 transcript) | **記「世界事件」的 hook**(ToolResult/Monitor/服務掛) |
| 日記排程 | `kernel.py:386-387`(`DIARY_HOUR=23`/`MINUTE=0` **寫死**)、`kernel.py:1101-1125`(`_diary_scheduler` 發泛型 `ScheduledMoment` + intent 命令) | 搬進 config + 改發**獨立 `DiaryMoment`** |
| 工具收窄 registry | `_active_tool_registry`(`mind_loop.py:839-919`;`_is_reflection`/`_is_agenda` 前例) | 加 `_is_diary` 分支 → `DIARY_TOOLS` |
| 感知類型 Literal | `Perception.kind`(`mind_state.py`,§2.3 已加 "AgendaMoment") | 加 "DiaryMoment" |
| prompt 條件注入 | `render_mind`(`mind_prompt.py:38-`,`pulse_block`/`cognition_block` 前例) | 加 `[Today's log]` block(僅日記回合) |
| 讀檔 inline 餵 pattern | `consolidation.py:46-50`(讀 `transcripts/{date}.md`、`[-tail_chars:]`) | 日記讀當天 log 照抄 |
| `WriteDiary` | `tools.py:146-176`(寫 `## <heading> 日記`、index);`EXTERNAL_TOOLS` 已含 | tool 現成,幾乎不動 |
| `Mood` | `mind_state.py:41-44`(`emotion: str` + `reason: str`,**自然語言非數值**) | 「實質變化」= `emotion` 字串變了 |
| config 類別樣板 | `config.py`(`BridgeConfig:139`/`McpConfig:165` = `BaseModel`) | 加 `DiaryConfig` |
| agenda 回合抑制發話 | 自主議程(positioning §2.3)`_emit_sentence` 抑制 agenda-turn speech | 日記回合沿用(日記私密,不對外講) |

---

## 2. 設計(三塊 + 延後一塊)

### 2.1 transcript 長成 action log

`memory_writer.py` 加一個 **action 寫入路徑**(與 `append_transcript` 同檔 `transcripts/{today}.md`、同 best-effort
index),寫一行 `- HH:MM:SS 我 <動作短語>`(跟對話行 `我說：…` 視覺上並列、可讀、可被 recall/consolidation 讀到)。

**兩個 call site(同一個 helper,兩處呼叫)**:

**(a) 她的動作 —— `_dispatch_tool`(mind_loop.py:1400 附近)**:`dispatch_one` 回傳後,若 `name` 在**白名單**,
記一行。白名單 + 摘要規則:

| 工具 | 記法 |
|---|---|
| Shell | `跑了指令: <cmd 首行截斷> → <r 摘要>` |
| SpawnWorkflow | `派了 workflow: <目標截斷>` |
| SpawnMonitor / RemoveMonitor | `設了/撤了 monitor: <目標>` |
| PursueGoal | `起了新目標:「<desc>」` |
| AdvanceGoal | `推進了目標:「<progress 截斷>」` |
| CloseLoop | `收掉了:「<desc/id>」` |
| WriteSchedule | `替未來排了: <when> <what>` |
| SelfRevision | `批准了自我的改變: <摘要>`(成長事件) |
| PinSelf | `整理了自我: <pin/prune 摘要>` |
| **NoteMemory**(輕指標) | `記下了: <note 首 ~40 字>`(**不塞全文** —— 全文本來就進記憶檔) |
| **MoodTool**(僅實質變化) | 僅當 MoodTool 真的**改了** emotion 才記 `心情變成「<emotion>」: <reason 截斷>`。**偵測機制**:`_dispatch_tool` 對 MoodTool 在 `dispatch_one` **之前**快照 `state.mood.emotion`,之後比對 —— 新 emotion ≠ 舊 emotion 才記(避免每次 Mood 呼叫都記)。 |

**跳過**(不記,防 recall 污染 + consolidation 撐爆):Recall、WriteDiary(meta,不記寫日記本身)、Report、Scratchpad、
NoteToolLesson、LearnName、以及任何 speech(naked text)——對話行已由現有路徑記。

**(b) 世界事件 —— 感知攝入迴圈(mind_loop.py:348-370,現有寫對話 transcript 的同區)**:對這些 `p.kind` 記一行:
- `ToolResultArrived`(Shell/Workflow 結果回來)→ `<Shell/Workflow> 結果回來: <summary 截斷>`
- `MonitorTriggered` → `Monitor「<label>」觸發: <line 截斷>`;`MonitorExited` → `Monitor「<label>」結束`
- `BridgeDown` / `MCPDown`(服務掛)→ `<service> 掛了`

**跳過**:UserSpoke/ChannelMessage(對話行已記)、DiaryMoment/AgendaMoment/ReflectionMoment/ScheduledMoment
(內部喚醒觸發,非世界事件)。

**選擇性守則(核心不變式)**:只記上表白名單。高頻內部 micro-op(每次 Recall、每次讀 mood)結構上不進 log ——
這是防「污染 same-day recall + 撐爆 consolidation tail」的閘。

### 2.2 日記讀當天的 log

日記回合(§2.3)在 render 時多注入一個 `[Today's log]` block:讀 `transcripts/{today}.md`、`[-tail_chars:]`
(照 `consolidation.py:50`;`tail_chars` 進 `DiaryConfig`,default 較大如 12000 以容一整天),inline 餵進**她自己那回合的
prompt**(不是像 consolidation 派 subagent —— 日記必須是**她第一人稱**寫)。她從「看著真實的一天」寫,不再是
20 條 recency 鑰匙孔。`WriteDiary` tool 本身不動;動的是「寫之前把當天攤在她面前」。

`render_mind` 加一個 optional `today_log_block: str | None`(照 `pulse_block`/`cognition_block` 前例),
非 None 才渲染 `[Today's log]`。

### 2.3 時機:config 化每日 deadline + 專用日記回合(v1)

**config 化**:kernel 寫死的 `DIARY_HOUR/MINUTE` 搬進新的 `DiaryConfig`(`config.py`):

```
[diary]
enabled = true          # 關掉 = 不排程日記回合
hour = 23               # 每日觸發時刻(取代寫死的 23:00)
minute = 0
tail_chars = 12000      # [Today's log] 注入的當天 log 尾長
```

**獨立感知**:`_diary_scheduler` 改發 `Perception(kind="DiaryMoment")`(不再是泛型 ScheduledMoment + 命令 intent),
好讓 mind_loop 辨識這是日記回合。`Perception.kind` Literal 加 "DiaryMoment"。

**專用日記回合(= 「強制動作 deadline」的落地,不造假)**:mind_loop 對只含 DiaryMoment 的 batch 設
`_is_diary = True`(照 `_is_agenda` 的 all-one-kind 前例,避免吞掉共批的其他 internal turn):
- **工具收窄**:`_active_tool_registry` 加 `_is_diary` 分支(擺在 `_is_agenda` 附近,同為 internal 自發回合)→ 回
  `DIARY_TOOLS = frozenset({"WriteDiary", "Recall"})`。她手上是「攤開的一天 + 寫日記 + 需要就 Recall 挖深」——
  那回合**自然的完成方式就是寫日記**,結構上逼近必寫。
- **無 fallback**:她沒呼叫 WriteDiary 就是沒寫(記一筆或 log warning),**系統不代生假日記**(守 no-fallback 鐵律 +
  假日記當訓練資料/內心生活零價值)。
- **私密**:日記回合**抑制對外發話**(沿用自主議程〔positioning §2.3〕agenda-turn 的 `_emit_sentence` speech 抑制)——日記寫進檔,不對使用者廣播。
- **觸發措辭**:從命令改成日終的空間感(render 的 DiaryMoment 敘述:「今天結束了。這是你的一天〔攤在
  [Today's log]〕。這是你寫日記的時間。」)—— obligation 但有尊嚴,對齊 Self-First(framing 非命令)。

### 2.4 延後(v2):睡後 trigger

由外部訊號(OS suspend/resume 或定義的 sleep 週期)觸發日記/consolidation,對齊 sleep-time-compute。
需外部訊號 plumbing,**本 spec 不做**,列此存念。

---

## 3. 安全 / 失敗模式

- **action-log 寫失敗不能拖垮 tool/turn**:照現有 `append_transcript` 的 best-effort `try/except`(mind_loop.py:380
  已有前例)——log 寫爆只記 exception、continue,永不中斷她的行動或回合。
- **recall / consolidation 污染**:白名單(§2.1)是唯一防線;跳過 Recall/內部 micro-op。NoteMemory 只輕指標、
  Mood 只實質變化,進一步壓低量。
- **無 fallback**(no-fallback 鐵律):日記沒寫成不代生;action-log 缺一行不補。
- **DIARY_TOOLS 是安全子集**:`{WriteDiary, Recall}` = 寫自己的日記 + 唯讀檢索,無 Shell/Workflow/外部動作。日記回合
  永遠 `origin_tier="internal"`(自發),收窄是「聚焦」不是「降權」,無安全張力。
- **origin 隔離**:action log 寫進 `transcripts/`(既有路徑);external_public 回合的動作若有(strangers 幾乎無動作可記)
  照現有 transcript 的 origin 處理,不新增洩漏面(本 spec 不碰 external 記錄範圍)。

## 4. Non-goals

- **不新做 action-log 流**(DRY:長現有 transcript)。
- **不改 `WriteDiary` tool 本身的語意**(仍第一人稱 prose)。
- **不做 v2 睡後 trigger**(延後)。
- **不做自發/中途寫日記**(v1 純每日 deadline;中途只看得到半天,YAGNI)。
- 不碰 consolidation/演化的邏輯(它們讀更豐富的 transcript 是**免費受惠**,非本 spec 改動)。
- 不做 external_public 動作記錄範圍的重新設計。

## 5. 測試策略

- **action-log helper**:白名單各工具記出正確一行;白名單外(Recall/WriteDiary/Report)**不記**;NoteMemory 只輕指標
  (不含全文);Mood 只在 emotion 變化時記、同 emotion 連續呼叫只記一次;寫失敗 try/except 不拋。
- **世界事件**:ToolResultArrived/MonitorTriggered/服務掛各記一行;內部喚醒觸發(DiaryMoment/AgendaMoment…)不記。
- **DiaryMoment 回合**:`_is_diary` 僅在 batch 全為 DiaryMoment 時 True(共批 UserSpoke → False,不被收窄);
  `_active_tool_registry` 回 `DIARY_TOOLS`;render 有 `[Today's log]`(= 當天 transcript tail);對外發話被抑制。
- **config**:`[diary].enabled=false` → 不排程;`hour/minute` 生效(取代寫死 23:00);`tail_chars` 生效。
- **向後相容**:action log = 現有 transcript 檔的**超集**,舊檔(純對話)照樣 consolidate/recall;`DiaryConfig` 缺省
  (舊 config.toml 無 `[diary]`)→ 沿用 default(enabled/23:00)。
- **Live-smoke(dogfood)**:真跑一天 → 23:00 日記回合讀得到當天動作(Shell/議程/心情)、寫出的日記反映真實一天而非只近況;
  trace 稽核日記內容對得回 `[Today's log]`。

## 6. 單概念 Task 拆解(給 SDD)

1. **action-log 寫入 helper**(`memory_writer.py`):一個 append-action 函式(同檔同格式、best-effort index)+ 摘要/截斷
   工具。測試:格式、寫失敗不拋、index 呼叫。
2. **記她的動作 @ `_dispatch_tool`**:白名單 + 各工具摘要規則 + NoteMemory 輕指標 + Mood 實質變化偵測(dispatch 前快照 emotion、後比對)。
   測試:§5 第一組。
3. **記世界事件 @ 感知迴圈**:ToolResult/Monitor/服務掛記一行;內部喚醒跳過。測試:§5 第二組。
4. **`DiaryConfig` + config 化排程 + `DiaryMoment`**:加 `DiaryConfig`(enabled/hour/minute/tail_chars)、`_diary_scheduler`
   讀 config 並發 `DiaryMoment`、`Perception.kind` 加 "DiaryMoment"。測試:config 生效、enabled=false 不排程、發對 kind。
5. **專用日記回合**:`_is_diary`(all-DiaryMoment)、`_active_tool_registry` 加分支回 `DIARY_TOOLS`、`render_mind` 加
   `[Today's log]` 注入(讀當天 transcript tail)、日記回合抑制發話、DiaryMoment 敘述措辭。測試:§5 第三組。承重(收窄
   registry + 私密)→ **opus 審**。

依序 1→2→3→4→5(5 依賴 4 的 DiaryMoment、依賴 1 的 log 存在)。merge 前 whole-branch 審 + full suite。

## 7. 開放決策(spec review 確認)

1. **白名單邊界**(§2.1 表):RemoveMonitor / PinSelf 要不要記?目前傾向記(都是「留下痕跡的動作」)。確認或刪減。
2. **`[Today's log]` tail 長度**:default 12000 字夠一整天嗎?過長會吃 diary 回合 context。可 config,先給 12000。
3. **日記回合措辭**:§2.3 的 DiaryMoment 敘述須是**空間/邀請**語氣(obligation 但非命令,對齊 Self-First),plan 定稿。
4. **v2 睡後 trigger** 確認延後、僅存念。
