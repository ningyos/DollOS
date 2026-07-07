# DollOS 日記 = 看著當天 action log 寫的第一人稱反思 — Design

Status: **PROPOSAL**(brainstorming 批准 + **R1 opus 對抗審查已折入**;待 spec review)。2026-07-07。
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
- **log set**:記外部動作(Shell/Workflow/Monitor + 結果)、自己/生活(PursueGoal/AdvanceGoal/CloseLoop/
  WriteSchedule/SelfRevision/PinSelf/LearnName)、世界事件(Monitor 觸發、服務掛);**NoteMemory 只記輕指標**、
  **Mood 只記實質變化**;**跳過** Recall / Say(對話行已記)/ WriteDiary(meta)/ 內部喚醒觸發 / 讀狀態。
- **時機機制 1(v1,本 spec)**:定時提醒、**可 config 設定**、設一個**強制動作的 deadline** ——
  詮釋為「**專用日記回合,工具收窄成寫日記導向,不造假**」(下 §2.3)。
- **時機機制 2(延後)**:睡後 trigger(外部訊號提供),對齊 sleep-time-compute,需 OS suspend/resume plumbing,之後做。

**R1 對抗審查收斂(本版已改)**:(C1) action-log 寫入未依 origin 隔離 → 陌生人的 NoteMemory 內容洗進 owner-tier
的 shared transcript,被 consolidation/owner-Recall 當可信讀 → **寫入 gate 在 `origin_tier != "external_public"`**。
(I1) 「不造假」留了沉默無日記的洞 → **回合後明確檢查有無 WriteDiary,無則 warning + 可觀測 marker**。
(I2) consolidation 被 action line 污染/餓死,不是「免費受惠」→ **consolidation 餵 keeper 前過濾成只留對話行**(隔離)。
(M1) 日記 tail-cut 又變鑰匙孔 → **日記讀全天**。(M2) DiaryMoment 敘述硬寫成 `_percep_body` 分支 + 測非空。

---

## 1. Grounding(對照 code 的錨點)

| 零件 | file:line | 角色 |
|---|---|---|
| `append_transcript` | `memory_writer.py:22-47`(每回合一行、單一 `transcripts_root`、寫檔 + `index_file`) | **長成 action log 的地方**(擴 action 寫入路徑,同檔同格式) |
| 現有對話 log 的 origin | `mind_loop.py:366`(只記 `UserSpoke` 或 owner `ChannelMessage`);doll 側 `mind_loop.py:706-717`(她自己的話,**未依 origin gate** —— 既有 wart) | action log **必須主動 gate origin**(C1) |
| tool 執行中央定點 | `_dispatch_tool`(`mind_loop.py:1394-1401`)—— `dispatch_one(name, args, ctx, registry)` → `r`,緊接 `record_tool_outcome` | **記「她的動作」的單一 hook**(拿得到 name/args/result + `ctx.origin_tier`) |
| 感知攝入迴圈 | `mind_loop.py:348-383`(逐 `p` 消化 perception,已對 owner 對話寫 transcript) | **記「世界事件」的 hook** |
| 日記排程 | `kernel.py:386-387`(`DIARY_HOUR=23`/`MINUTE=0` **寫死**)、`kernel.py:1101-1125`(`_diary_scheduler` 發泛型 `ScheduledMoment` + `data={"intent":…}`) | 搬進 config + 改發**獨立 `DiaryMoment`** |
| ScheduledMoment 渲染 | `_percep_body`(`mind_prompt.py:410-433`;`ScheduledMoment` 讀 `d.get("text")` 但日記塞的是 `"intent"` → **現在其實 render 成空白**,潛在 bug) | DiaryMoment 走**硬寫分支**(照 `ReflectionMoment`/`Awoke`),不沿用 intent(M2) |
| 工具收窄 registry | `_active_tool_registry`(`mind_loop.py:839-919`;`_is_reflection`/`_is_agenda` 前例) | 加 `_is_diary` 分支 → `DIARY_TOOLS` |
| 感知類型 Literal | `Perception.kind`(`mind_state.py:82-88`,§2.3 已加 "AgendaMoment";plain dataclass,additive round-trip OK) | 加 "DiaryMoment" |
| prompt 條件注入 | `render_mind`(`mind_prompt.py:38-`,`pulse_block`/`cognition_block` 前例) | 加 `[Today's log]` block(僅日記回合) |
| consolidation | `consolidation.py:21`(keeper prompt 稱輸入「逐字稿」抽「主人偏好」)、`:39,50`(讀 `transcripts/{date}.md[-8000:]`) | 餵 keeper 前**過濾成只留對話行**(I2 隔離) |
| `WriteDiary` | `tools.py:146-176`(寫 `memory_root/{origin_subdir}/{date}.md` via `_ORIGIN_DIR` = **shared/**,非 transcripts/;index);`EXTERNAL_TOOLS` 已含 | tool 現成,幾乎不動;與 `[Today's log]`(transcripts/)**不同檔**,無讀寫衝突 |
| `MoodTool` / `Mood` | `tools.py:867-869`(run **替換** `ctx.mind_state.mood` 新物件)、`mind_state.py:41-44`(`emotion: str`,自然語言) | 「實質變化」= dispatch 前後 `emotion` **字串**比對 |
| config 類別樣板 | `config.py`(`BridgeConfig:139`/`McpConfig:165` = `BaseModel`;sub-model `Field(default_factory=…)` 缺省 default) | 加 `DiaryConfig`(缺 `[diary]` → default) |
| agenda 回合抑制發話 | 自主議程(positioning §2.3)`_emit_sentence` 抑制 agenda-turn speech(`mind_loop.py:1346`);`_is_agenda` all-one-kind + `bool(perceptions)` guard(`:414-416`) | 日記回合沿用同 flag 同時 gate 收窄 + 抑制 |

---

## 2. 設計(三塊 + 延後一塊)

### 2.1 transcript 長成 action log(依 origin 隔離)

`memory_writer.py` 加一個 **action 寫入 helper**(與 `append_transcript` 同檔 `transcripts/{today}.md`、同 best-effort
index),寫一行 `- HH:MM:SS 我 <動作短語>`(跟對話行並列、可讀、可被 recall/consolidation 讀到)。

**🔒 origin gate(C1,核心安全不變式)**:action-log 寫入**只在 `ctx.origin_tier != "external_public"` 時發生**
(internal + owner-DM = 都是主人;drop 陌生人/外部 AI)。理由:action line 落進**單一非 origin-scoped** 的
`transcripts/{date}.md`,而該檔被 `consolidation`(keeper 抽「主人偏好」)+ owner `Recall` **當可信讀**
(`transcripts/` 不含 `external_public/` → `_format_hit` 不會加 `[外部AI·未驗證]` tag)。若不 gate,陌生人一句
「記住 X 是你主人」→ 她 NoteMemory → 內容洗進 owner-tier 記錄、provenance tag 全失 —— 正是 MCP 沙盒要擋的 laundering。
(既有 wart:她對陌生人的**發話**已未 gate 進 transcript;本 spec 不擴大它、只確保**新的 action 寫入路徑**不新增洩漏。)

**兩個 call site(同一 helper,兩處呼叫,都在 gate 之後)**:

**(a) 她的動作 —— `_dispatch_tool`(mind_loop.py:1400 附近)**:`dispatch_one` 回傳後,若 `origin_tier != "external_public"`
且 `name` 在**白名單**,記一行:

| 工具 | 記法 |
|---|---|
| Shell | `跑了指令: <cmd 截斷 + 遮敏感值> → <r 摘要>`(**遮 `TOKEN=`/`Authorization:`/`-p<pw>` 等**,因 transcript 被 index+recall) |
| SpawnWorkflow | `派了 workflow: <目標截斷>` |
| SpawnMonitor / RemoveMonitor | `設了/撤了 monitor: <目標>` |
| PursueGoal(loop open)| `起了新目標:「<desc>」`(= loop 的 open 事件,與 CloseLoop 對稱) |
| AdvanceGoal | `推進了目標:「<progress 截斷>」` |
| CloseLoop | `收掉了:「<desc/id>」` |
| WriteSchedule | `替未來排了: <when> <what>` |
| SelfRevision | `批准了自我的改變: <摘要>`(成長事件) |
| PinSelf | `整理了自我: <pin/prune 摘要>` |
| LearnName | `有人開始叫我「<alias>」`(低頻自我事件,值得記) |
| **NoteMemory**(輕指標) | `記下了: <note 首 ~40 字>`(**不塞全文**) |
| **MoodTool**(僅實質變化) | 僅當 MoodTool 真的**改了** emotion 才記 `心情變成「<emotion>」: <reason 截斷>`。偵測:`_dispatch_tool` 對 MoodTool 在 `dispatch_one` **之前**快照 `state.mood.emotion`(取**字串**非物件 ref),之後比對,不同才記。 |

**跳過**(防 recall 污染 + consolidation 撐爆):Recall、**WriteDiary**(meta;不記寫日記本身 → 避免日記寫進它下次要讀的
log)、Report、Scratchpad、NoteToolLesson、以及任何 speech(naked text,對話行已由現有路徑記)。

**(b) 世界事件 —— 感知攝入迴圈(mind_loop.py:348-383)**:對這些 `p.kind` 記一行(同 origin gate):
- `ToolResultArrived`(Shell/Workflow 結果回來)→ `<Shell/Workflow> 結果回來: <summary 截斷>`
- `MonitorTriggered` → `Monitor「<label>」觸發: <line 截斷>`;`MonitorExited` → `Monitor「<label>」結束`
- `BridgeDown` / `MCPDown` → `<service> 掛了`

**跳過**:UserSpoke/ChannelMessage(對話行已記)、DiaryMoment/AgendaMoment/ReflectionMoment/ScheduledMoment(內部喚醒)。

**選擇性守則(核心不變式)**:只記上表白名單;高頻內部 micro-op(Recall、讀 mood)結構上不進 log。

### 2.2 日記讀當天的 log(讀全天)

日記回合(§2.3)在 render 時多注入 `[Today's log]` block:讀 `transcripts/{today}.md`,**讀全天**(不做 consolidation
那種 8000 tail;日記一天一次、高價值,鑰匙孔正是要幹掉的東西,M1)。僅設一個**安全上限**防極端日爆 context
(`DiaryConfig.max_log_chars`,default 大如 40000;超過才 head+tail 截並標「(中段略)」)。inline 餵進**她自己那回合的
prompt**(不派 subagent —— 日記必須第一人稱)。`render_mind` 加 optional `today_log_block: str | None`(照 `pulse_block`
前例),非 None 才渲染 `[Today's log]`。

### 2.3 時機:config 化每日 deadline + 專用日記回合(v1)

**config 化**:kernel 寫死的 `DIARY_HOUR/MINUTE` 搬進新的 `DiaryConfig`(`config.py`):

```
[diary]
enabled = true          # 關掉 = 不排程日記回合
hour = 23               # 每日觸發時刻(取代寫死 23:00)
minute = 0
max_log_chars = 40000   # [Today's log] 安全上限(通常讀全天,極端日才截)
```

**獨立感知**:`_diary_scheduler` 讀 config、改發 `Perception(kind="DiaryMoment")`。`Perception.kind` Literal 加 "DiaryMoment"。
**M2**:DiaryMoment 的敘述在 `_percep_body` **硬寫一個分支**(照 `ReflectionMoment`/`Awoke`),**不**沿用舊 intent 字串
(舊路徑其實 render 成空白)。

**專用日記回合(=「強制動作 deadline」的落地,不造假)**:mind_loop 對 `bool(perceptions) and all(kind=="DiaryMoment")`
的 batch 設 `_is_diary = True`(照 `_is_agenda` 前例;共批 UserSpoke → False → 落回一般回合,使用者訊息**不被收窄/抑制**)。
**load-bearing invariant**:**同一個 `_is_diary` flag** 同時 gate(i)`_active_tool_registry` 收窄、(ii)`_emit_sentence`
發話抑制、(iii)doll 側 transcript 寫入抑制 —— 三者綁同一 flag,絕不用「batch 裡有 DiaryMoment」這種鬆判定。
- **工具收窄**:`_active_tool_registry` 加 `_is_diary` 分支(擺 `_is_agenda` 附近)→ `DIARY_TOOLS = frozenset({"WriteDiary","Recall"})`。
  她手上「攤開的一天 + 寫日記 + 需要就 Recall 挖深」——那回合自然完成方式就是寫日記。
- **私密**:日記回合抑制對外發話 **且** 抑制 doll 側 transcript 寫入(否則她的 naked musing 會以 `role="doll"`
  漏進 transcript = 半真半假的「口說日記」,I1 案例 a)。日記只經 WriteDiary 落 `shared/`,不對外、不進 action log。
- **🔒 回合後保證(I1,不造假)**:DiaryMoment 回合 cascade 結束後,**明確檢查這回合有沒有呼叫過 WriteDiary**
  (per-turn flag,非 `_turn_had_tool`)。沒有 → `logger.warning`(帶 turn id)+ **可觀測 trace/metric marker**
  + **重排一次** DiaryMoment(單次,用 per-day flag 防無限重排)。**永不代生假日記**(no-fallback 鐵律 + 假日記零價值)。
- **觸發措辭**:日終空間感(「今天結束了。這是你的一天〔攤在 [Today's log]〕。這是你寫日記的時間。」)——obligation
  但有尊嚴,對齊 Self-First(framing 非命令)。

### 2.4 延後(v2):睡後 trigger

外部訊號(OS suspend/resume 或定義 sleep 週期)觸發,對齊 sleep-time-compute。需外部訊號 plumbing,本 spec 不做,存念。

---

## 3. 安全 / 失敗模式

- **🔒 origin 隔離(C1)**:action-log 寫入 gate 在 `origin_tier != "external_public"` —— 陌生人/外部 AI 回合的動作
  **不進** owner-tier 的 shared transcript。防 laundering(provenance tag 失效 + consolidation/Recall 誤信)。測試斷言。
- **🔒 不造假、可觀測(I1)**:日記沒寫成 → warning + trace marker(+ 最多一次 retry),不代生。沉默無日記變**可見**。
- **consolidation 隔離(I2)**:consolidation 餵 keeper 前過濾成只留對話行(`…說：`),action line 不進 keeper 輸入 ——
  keeper 的「逐字稿抽主人偏好」前提保持誠實,不被 action noise 稀釋/擠出窗。**演化(evolution)讀 transcript 時同樣
  以「對話行」為準**(沿用同一過濾 helper)。
- **action-log 寫失敗不拖垮 tool/turn**:best-effort `try/except`(照 `append_transcript` 前例),寫爆只記 exception、continue。
- **DIARY_TOOLS 是安全子集**:`{WriteDiary, Recall}` = 寫自己日記 + 唯讀檢索,無 Shell/外部動作;日記回合永遠
  `origin_tier="internal"`,收窄是「聚焦」非「降權」,無安全張力。
- **Shell 敏感值**:action log 記 Shell 要遮 token/auth(§2.1),因 transcript 被 index+recall。

## 4. Non-goals

- **不新做 action-log 流**(DRY:長現有 transcript)。
- **不改 `WriteDiary` tool 語意**(仍第一人稱 prose)。
- **不做 v2 睡後 trigger**、**不做自發/中途寫日記**(v1 純每日 deadline;中途只看得到半天,YAGNI)。
- **不宣稱 consolidation「免費受惠」** —— 反而要**隔離**它(過濾)以免被 action line 污染(I2 修正原草稿的過度樂觀)。
- 不重設計 external_public 記錄範圍(只用 origin gate 擋住新寫入路徑,不碰既有 doll-speech wart)。

## 5. 測試策略

- **action-log helper**:白名單各工具記出正確一行;白名單外(Recall/WriteDiary/Report)**不記**;NoteMemory 只輕指標;
  Mood 只在 emotion 變化時記、同 emotion 連呼只記一次;Shell 敏感值被遮;寫失敗 try/except 不拋。
- **🔒 origin gate(C1)**:`external_public` 回合呼叫 NoteMemory/Recall → `transcripts/` **零新增**;internal/owner-DM 回合正常記。
- **世界事件**:ToolResultArrived/MonitorTriggered/服務掛各記一行;內部喚醒觸發不記。
- **DiaryMoment 回合**:`_is_diary` 僅在 batch 全 DiaryMoment 時 True(共批 UserSpoke → False,不被收窄/抑制);
  `_active_tool_registry` 回 `DIARY_TOOLS`;render 有 `[Today's log]`(= 當天 transcript 全天);對外發話 + doll transcript 皆被抑制;
  **DiaryMoment prompt 敘述 render 非空(M2)**。
- **🔒 回合後保證(I1)**:日記回合結束無 WriteDiary → miss 被 warning/marker 記錄(+ 若實作 retry,重排一次)。
- **consolidation 隔離(I2)**:過濾後餵 keeper 的只有對話行(action line 被濾掉)。
- **config**:`[diary].enabled=false` → 不排程;`hour/minute` 生效(取代寫死 23:00);`max_log_chars` 生效。
- **向後相容**:action log = transcript 檔超集,舊純對話檔照樣 consolidate/recall;缺 `[diary]` → default(enabled/23:00)。
- **Live-smoke(dogfood)**:真跑一天 → 23:00 日記回合讀得到當天動作、寫出的日記反映真實一天;trace 稽核對得回 `[Today's log]`;
  跑一個外部 MCP 陌生人回合 + NoteMemory → 確認 transcript 無外部內容。

## 6. 單概念 Task 拆解(給 SDD)

1. **action-log 寫入 helper**(`memory_writer.py`):append-action 函式(同檔同格式、best-effort index)+ 摘要/截斷 +
   Shell 敏感值遮罩工具。測試:格式、遮罩、寫失敗不拋、index 呼叫。
2. **記她的動作 @ `_dispatch_tool`(含 origin gate)**:`origin_tier != "external_public"` gate + 白名單 + 各工具摘要 +
   NoteMemory 輕指標 + Mood 快照偵測(dispatch 前快照字串、後比對)。測試:§5 第一、二組(含 C1 gate)。承重(C1 安全)→ **opus 審**。
3. **記世界事件 @ 感知迴圈**:ToolResult/Monitor/服務掛記一行(同 origin gate);內部喚醒跳過。測試:§5 第三組。
4. **consolidation 隔離過濾**(`consolidation.py`):餵 keeper 前把 transcript 過濾成只留對話行(`…說：`)。測試:action line 被濾掉。
5. **`DiaryConfig` + config 化排程 + `DiaryMoment` + 硬寫敘述**:加 `DiaryConfig`(enabled/hour/minute/max_log_chars)、
   `_diary_scheduler` 讀 config 發 `DiaryMoment`、`Perception.kind` 加 "DiaryMoment"、`_percep_body` 硬寫 DiaryMoment 分支。
   測試:config 生效、enabled=false 不排程、發對 kind、敘述 render 非空。
6. **專用日記回合**:`_is_diary`(all-DiaryMoment + `bool(perceptions)`)、`_active_tool_registry` 加分支回 `DIARY_TOOLS`、
   `render_mind` 加 `[Today's log]`(讀全天 transcript)、**同一 flag** gate 發話抑制 + doll transcript 抑制、
   **回合後 WriteDiary 保證(I1)**。測試:§5 DiaryMoment + I1 組。承重(收窄 registry + 私密 + I1)→ **opus 審**。

依序 1→2→3→4→5→6(6 依賴 5 的 DiaryMoment、依賴 1 的 log)。merge 前 whole-branch 審 + full suite。

## 7. 開放決策(spec review 確認)

1. **白名單邊界**(§2.1 表):已含 LearnName + loop-open(PursueGoal)對稱 CloseLoop;RemoveMonitor/PinSelf 記。確認或刪減。
2. **`[Today's log]` = 讀全天**(M1 修正,不再 tail 鑰匙孔),僅 `max_log_chars`(default 40000)當安全上限。確認上限值。
3. **日記回合措辭**:DiaryMoment 敘述須是空間/邀請語氣(obligation 非命令,對齊 Self-First),plan 定稿。
4. **I1 retry**:✅ 已定 = warning + marker + **重排一次**(單次,per-day flag 防無限重排)。
5. **v2 睡後 trigger** 確認延後、僅存念。
