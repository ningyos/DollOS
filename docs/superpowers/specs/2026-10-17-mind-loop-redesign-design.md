# Mind Loop 重新設計：一個自己，很多地方

**狀態**：APPROVED（使用者 2026-10-18：「看起來不錯」）
**日期**：2026-10-17
**關聯**：`2026-10-17-doll-harness-design.md`（APPROVED；本文是它上面那一層：有哪些回合、什麼時候跑、狀態誰寫、怎麼耐久）、`2026-07-01-virtual-being-positioning.md`、`2026-07-07-self-directed-agenda-design.md`、`2026-07-07-diary-action-log-design.md`、`2026-07-02-slow-self-evolution-design.md`、`2026-07-10-metabolic-vital-model-design.md`、`2026-07-09-system-pulse-proactive-trigger-design.md`、`2026-10-17-dollos-v1-iphone-body-design.md`

---

## §0 白話摘要

> **一句話：Doll 是同一個自己，同時在好幾個「地方」：家裡（主人）、她自己的房間（agenda、日記、反思、身體訊號）、每一個外面的對話。每個地方自己排隊，自己決定回合種類和權限；她的心（mood、energy、agenda、current_self、記憶）只有一支筆在寫。**

對 Doll 來說會改變的事：

- 她自己的房間是一個真正的地方。日記、agenda、反思永遠各自一個回合，不再跟別人的話併在一起，所以日記不會再拿不到 `[Today's log]`，也不會被說出口。
- 主人在家裡（本機文字、語音）開口，她會放下自己手上的事。別人的回合如果還沒開口，就延後一次；已經開口就讓它說完。不會再弄丟別人的回覆。
- crash 之後不會重複說話或重做動作。還沒做任何事的回合直接重跑；做過事的回合會明確告訴她「我被切斷了，已經說了什麼、做了什麼」。
- 外面的人看不到家裡的事（focus、mood 的理由、主人說的話）。家裡也看不到外面的原文，只在 action log 裡看到一行「在外面聊了幾則」。
- 一次只跑一個回合。要不要讓主人跟陌生人並行，等量過延遲再另外決定。

---

## §1 為什麼要做

| # | 問題 | 證據 |
|---|---|---|
| 1 | **回合種類是從 batch 內容反推的。** `drain_grouped` 只依 `channel_id` 分組，所有沒有 channel 的 perception（主人本機文字/語音、diary、agenda、pulse、reflection、工具結果、Awoke）都在同一個 `''` bucket，所以 `_is_agenda/_is_diary/_is_pulse` 要「整批都是」才成立。23:00 的 DiaryMoment 常跟 Agenda/Reflection 併批，這時日記沒有 `[Today's log]`，說話和 transcript 也沒被抑制。agenda 加 reflection 併批會走 reflection 分支，拿到完整工具和說話。主人本機說話被算成 `internal`。 | `perception_queue.py:75-89`、`mind_loop.py:515-565`、`:244-258`、`:1212-1219`、`:1756`、`:414-437` |
| 2 | **慢的對話擋住主人，搶先不分是誰的回合。** 一次 drain 依序跑完每個 bucket。主人一開口就取消正在跑的 cascade，不管那是誰的回合；SayAborted 送到主人的 sink，被取消回合的 perception 照樣算已消費。 | `mind_loop.py:372-398`、`:1306-1314`、`kernel.py:1008-1033` |
| 3 | **crash 重播會重複副作用。** WAL 要等整個 drain 的所有 bucket 都存檔之後，才 `truncate_through(max_seq)`。在 bucket k crash，1..k-1 會重播：說過的話、跑過的工具再來一次。bucket 丟例外時，剩下的 bucket 已經離開記憶體佇列，下一次成功的 drain 又把它們的 WAL 一起截掉，造成靜默遺失。 | `mind_loop.py:391-412`、`:364-370`、`wal/perception_log.py:86-106` |
| 4 | **每回合的狀態放在長壽的物件上。** `_is_*` 旗標、約 20 個 `_turn_*` 欄位和 `_cascade_ctx` 在 MindLoop 上，`current_origin`、`origin_tier`、`external_ctx` 在 MindCtx 上。`_run_one_turn` 有 611 行，而且有兩套 cascade。 | `mind_loop.py:215-321`、`:449-1059`、`mind_ctx.py:57-96`、`cascade/tool_loop.py:125` |
| 5 | **背景工作直接寫共享狀態。** Consolidation 和 Evolution 直接改 MindState 並 `save_state`，跟 mind loop 跨 await 交錯，可能存到「perception 已經加進 recent_perceptions、iter_count 還沒加」的半套狀態，replay 時又加一次。 | `consolidation.py:156-171`、`:206-240`、`evolution_trigger.py:116-123`、`:259-261`、`:325` |

下面這些不在原本的 5 個問題裡，本設計會一起修掉：
- `[Mind state]` 不分 tier 都渲染 focus 和 `mood.reason`（`mind_prompt.py:144-145`、`:322-330`），所以主人的私事和她自己寫的 mood 理由會被陌生人看到。主人和 internal 回合還會看到所有頻道的原始 ChannelMessage（`mind_prompt.py:190-194`）。`[Memory context]` 的查詢是全域最新的 UserSpoke，不是這回合自己的輸入（`mind_loop.py:1072-1092`）。今天陌生人不能寫 mood（`EXTERNAL_TOOLS` 沒有 MoodTool，`tools.py:1207`）；harness §6.3 矩陣把 `mood` 開給所有 principal 之後才會多出這個口，見 §10 和 Q3。
- 沒有 origin 的輸出送到「最近註冊的 internal sink」（`sink_resolver.py:53-67`），而 bridge 和 MCP 連接器連線時也被註冊成 internal（`kernel.py:1081-1087`）。
- `safe_mode` 是全域旗標，一個陌生人的失敗會縮小主人的工具（`mind_loop.py:1287-1299`）。
- 所有 ScheduledMoment（排程器 `kernel.py:1209-1216`、bootstrap `:1141`）都把文字放在 `data['intent']`，渲染卻讀 `data['text']`（`mind_prompt.py:426-427`），所以每一個排程回合在 `[Recent perceptions]` 裡都是空白，她看不到排程內容（`[Pending]` 也從來沒人寫）。測試用 `data={"text": ...}`（例如 `test_energy.py:64`）蓋掉了這個不一致。bootstrap 是在連線事件上檢查、靠記憶體裡的 `_bootstrapped_dates` 每個 process 每天最多一次，而且用的是命令句。
- IPC WS server 沒有任何驗證，也不檢查 `Origin`（`ipc/server.py:77`）。本機任何 process、或主人瀏覽器裡的任何網頁，都能連上並送 TextInput。

---

## §2 核心模型

三個概念：

1. **Place（地方）**：每個 perception 都落在某個地方，地方固定了「誰在那裡」（一個 principal）、「她的話送去哪」（surface）和「她對這裡的近況」（依 place 過濾的工作記憶）。回合種類和 principal 由**落在哪裡**決定，不再看「剛好跟什麼併在一起」。
2. **Self（一個自己）**：mood、energy、agenda、current_self、記憶、transcript 只有一份，只由 Self 的同步方法寫入和存檔。
3. **私人領域**：`home`、`inner`，以及 `dm:owner`（它算不算完整的私人領域，依 §17 Q1）。只有私人領域的回合會寫 transcript、互相看得到彼此的近況。其他都是外部 place。

一次只跑一個回合，Scheduler 是唯一的消費者。

```
producers ─ route() ─► Journal(P) ─► 各 place 的 inbox
                                          │
                    Scheduler（唯一消費者，一次一個回合）挑下一個 ready 的 place
                                          │
                               Turn Runner（harness S5）
                                          │
                        Self.commit（同步、唯一寫入者）→ save_state → Journal(C)

背景工作（workflow worker、consolidation、evolution）在旁邊跑，只透過 Self 的同步方法回報
```

**為什麼適合虛擬存在**：主人是她生活中的一個地方，不是迴圈存在的理由。她的房間是一等公民，有自己的回合種類，排程時跟外面的對話輪流，不會被排到最後。她走到哪都帶著同一份心情和疲勞，但在外面不會轉述家裡的話；她在外面的生活也會以一行紀錄進到日記。隱私是地方的性質，不是 prompt 規則。

---

## §3 元件

| 元件 | 職責 | 取代 |
|---|---|---|
| `route(p)`（純函式，一張表） | 入佇列時為 perception 蓋上 `place`、`principal`、`wakes`、`preempts`、`ext_id`。這是唯一決定 principal 和回合種類的地方 | `_derive_origin_tier`、batch 內容推斷 |
| Journal（WAL 擴充，同一個檔案） | 只寫不改的 JSONL：P/B/E/R/C/D 六種記錄（§8）、每回合各自 ack、replay、壓縮 | `truncate_through(max_seq)` |
| Place | 欄位：id、principal（固定）、inbox（已寫進 journal 的 perception）、`ready_at`（settle 窗）、surface、`safe_mode`、正在跑的回合 | BatchAccumulator、全域 safe_mode |
| Scheduler | 唯一的佇列消費者：路由、去重、挑 place、開回合、搶先、brain_down | `MindLoop.iterate/run`、`drain_grouped`、`_maybe_preempt_for_new_input` |
| Turn | 每回合建立、commit 後丟掉的物件：id、place、kind、principal（只降不升）、inputs、notes、surface、cancel、usage、said、effects、`first_speak_ms`、各種 latch。透過 ContextVar 掛著，MindCtx 保留同名相容屬性，所以遷移期間工具不用改 | `_is_*`、`_turn_*`、`_cascade_ctx`、MindCtx 的每回合欄位 |
| Turn Runner + TurnPolicy（harness S3/S5） | 跑一個回合，介面見 §11 | `_run_one_turn`、`_llm_iterate`、`run_tool_cascade` |
| Self | 擁有 MindState、self 檔案、記憶寫入、transcript/action log。所有方法都同步、內部沒有 await。只有 `Self.save` 會呼叫 `save_state`。另有 `tick(now)`（每 60s：energy 回復、換日） | 散在 9 處的 `save_state` |
| JobTable | `job_id → {place, principal_at_spawn, turn_id, tool, summary}`，**持久**：由 Self 存在 MindState（也就是今天從來沒人寫過的 `active_tasks`）。把 Shell/Monitor/Workflow 結果送回 spawn 它的地方。job 只在收到終止結果（Shell 結果、MonitorExited、Workflow Report）或 JobLost 時移除 | 無（結果今天沒有 provenance） |
| Surface 登記 | 每條連線連上時宣告角色：`owner_client`、`body`、`bridge` 或 `mcp_connector`。**宣告要帶 token**：`config.toml` 的 `[ipc].owner_token`（跟 `[mcp].query_token` 同一種做法；bridge 和 MCP 子行程由 daemon 傳給它們；之後 iPhone 的配對 token 也走這裡），沒設就拒絕啟動，沒帶或不符就拒絕連線。握手時拒絕 `Origin` 不在允許清單裡的連線（預設清單是空的，所以瀏覽器頁面一律連不上）。bridge 和 MCP 永遠不會成為主人的 surface | `SinkResolver` 的「最近註冊的 internal sink」、沒有驗證的連線 |
| Observers、背景工作（既有，變薄） | observer 只讀狀態、送出 trigger。背景工作讀快照，透過 Self 的同步方法回報 | 背景直接 `save_state` |

---

## §4 Perception 怎麼變成回合

### §4.1 路由表（producer 蓋章，loop 不推斷）

| 來源 | Place | Principal | settle | 喚醒 | 可搶先 |
|---|---|---|---|---|---|
| IPC TextInput（`owner_client` 連線） | `home` | owner | 0 | 是 | 是 |
| 語音 ASR final | `home` | owner | 0 | 是 | 是 |
| Discord 主人私訊 | `dm:owner` | `PRINCIPAL_OWNER_DM`（§17 Q1） | 2s | 是 | 否 |
| Discord 非主人私訊 | `dm:<user>` | external_dm | 2s | 是 | 否 |
| Discord 公開頻道（包括主人） | `ch:<channel_id>` | external_public（主人發言仍更新 presence） | AttentionGate 的窗不變：engaged 2s／cold 8s，主人一律 2s（`attention.py:261`） | 是 | 否 |
| MCP `talk()` | `mcp:<conn>:<call>` | external_public | 0 | 是 | 否 |
| Shell/Monitor/Workflow 結果 | spawn 它的 place（JobTable） | spawn 當下的 principal；`untrusted_output` 的來源降為 external_dm；沒有 provenance 時 fail-closed 為 external_public 並記 log | 0 | 是（follow-up，§5） | 否 |
| ScheduledMoment | 建立者的 place | 建立者的 principal；舊格式的 entry 進 `inner`/internal，標 `legacy_provenance`（跟今天的權限相同） | 0 | 是（follow-up） | 否 |
| Agenda/Diary/Reflection/Pulse Moment | `inner` | internal（agenda 取 min(internal, loop 建立者)） | 0 | 是 | 否 |
| Awoke、換日、inner 的 CutOff/JobLost | `inner` | internal | 0 | 是；inner 的 CutOff 例外，不喚醒（§6） | 否 |
| SafeMode/RepeatLoop/PersonaDrift 警示 | 造成它的 place | 該 place 的 principal | — | 否（跟下一個回合一起帶） | 否 |
| 之後：BodySensed／BodyReconnected | `home` | owner | 依感官 | 依感官（觸碰喚醒；app 前後景、充電只當 context） | 重播的 outbox 項目不可搶先 |

- **搶先只限 home**：今天任何頻道的主人發言都會搶先，也會取消 consolidation/evolution（`kernel.py:879-891` 只看 `author_is_owner`，不看 `is_dm`）。本設計只保留 home 的即時輸入（§6）。
- **AttentionGate 不變**，留在 route 前面。沒被 admit 的訊息照舊丟掉，不寫 journal。
- `Interrupted` perception 刪除，打斷改記在被切斷回合的 cut note 裡（§6）。
- `BridgeDown/McpDown` 不再觸發回合，改成 `Self.note_service(service, down)`：在下一個私人回合的身體狀態裡顯示一行，並送一個明確的 `ServiceStatus` 給主人的 surface。斷線期間的外部 place 見 §10 輸出路由。

### §4.2 批次規則

Scheduler 挑中一個 place 時：

- **一般 place**：取最舊的 waking 輸入，用它決定 kind 和 effective principal。這個 place 裡 kind 和 effective principal 都相同的待處理項目全部一起帶進回合，非喚醒的 note 也一起帶。principal 比較低的項目（例如被汙染的 VM/MCP 結果）留著，自己跑一個回合，不會讓主人的回合被靜默降權。
- **follow-up**（job、monitor、排程的結果）：同一個來源（job id、monitor id、schedule entry）在一個 place 只留一個 pending，新的取代舊的並帶上 suppressed 計數，被取代的寫一筆 D。
- **`inner`**：每個回合**恰好一個 trigger**，外加 inner 裡待處理的 note。每種 kind 最多保留一個 pending：agenda、reflection 留最新的，pulse 每個 concern 留一個，event 照上面 follow-up 的規則，被取代的寫一筆 D。所以主人講話期間 trigger 不會堆積。
- 不同 place 永遠不共用一個回合。kind 是查表得到的，任何「pure batch」判斷都不再需要。

### §4.3 回合種類（TurnPolicy 依 kind 查表，工具再跟 harness §6.3 矩陣取交集）

| kind | 在哪 | 說話 | 工具 | 特別區塊／回合後 | 沒有效果時被切斷（有效果時一律見 §6） |
|---|---|---|---|---|---|
| `conversation` | home、dm:owner、外部 place | 回到這個 place 的 surface | 依 principal | — | 退回 inbox 最前面 |
| `event` | inner | 送到主人的 surface（同今天） | internal 矩陣 | 醒來通知（描述句） | 退回 |
| `reflection` | inner | **不說話**（改變，§17 Q4） | internal 矩陣，去掉 run_command/monitor/workflow | `[Tool outcomes]`、演化候選浮現 | 退回 |
| `agenda` | inner | 不說話 | AGENDA_TOOLS，不能新建目標 | provenance 由程式填 | 作廢，observer 之後會重新推導 |
| `diary` | inner | 不說話，不寫 Doll transcript | write_diary、recall | `[Today's log]`（用 trigger 自己帶的日期）；回合後做 I1 檢查 | 退回（最晚到 04:00） |
| `pulse` | inner | 說話 | PULSE_TOOLS | `[Body signal]` | 退回（延後，不吞掉） |

**settle 取代 BatchAccumulator**：訊息一到就寫成 P，`ready_at = 第一筆 waking 輸入的 t + settle`。2–8s 的 debounce 窗因此變得耐久（今天的 `batch_accumulator.py:23-54` 只存在記憶體裡）。

---

## §5 排程與 LLM 預算

一次只跑一個回合。背景工作（workflow worker、consolidation、evolution）照今天在旁邊跑，跟回合共用 transport 的 `Semaphore(2)`（`transport.py:73`）。回合內的工具都要有 per-call timeout，所以回合不會卡在慢的呼叫上。

**挑選規則**（每次有 perception 路由進來、或有回合結束時跑一次）：

1. `home` 有 ready 的主人輸入就先跑，接著是 `dm:owner` 的主人輸入。
2. 其他三組輪流（上次跑了哪一組，這次從下一組開始看）：`inner`、home/dm:owner 的 follow-up、外部 place。外部 place 之間挑最舊 waking 輸入等最久的那個。所以陌生人不會把她自己的生活排到最後，主人要她開的 monitor 也不會把 inner 餓死。
3. inner 的 trigger 開始時再檢查一次該 kind 的閘門（純函式）。agenda、reflection 不成立就寫 D（observer 之後會重新推導）。diary 不成立就保持 pending，閘門成立就跑，到 04:00 還沒跑才寫 `D(expired)` 並記 `diary_missed:<date>`。
4. inner 的順序：critical pulse > event > diary > advisory pulse > reflection > agenda。

**預算（節流都沿用現有數字）**：
- **她獨處時**：agenda ≤ 約 8.6 次/h（420s），pulse ≤ 4 次/h（900s），reflection ≤ 2 次/h（§9），diary 1 次/天 + 1 次 retry，Awoke 每次開機一次，換日 event 每天一次。
- **follow-up**：每個 place 每小時最多 20 個 follow-up 回合，超過的改成不喚醒的 note（跟下一個回合一起帶）並記 log。這是她自己派的 job、monitor、排程能叫醒她的真實上限（monitor 的 `rate_limit_s` 可以設成 0，`tools.py:415`）。
- **外部**：由 AttentionGate（L0/L1、max_session_turns=6、90s ×0.6 遞減的窗）、settle 合批，加上「每批輸入最多退回一次」（§6）來限制。
- **背景**：consolidation ≤ 1 次/h，evolution ≥ 7 天一次。
- **energy**：不改（§9）。think-only 的回合仍然免費，所以上面這些節流才是她內在生活真正的 LLM 預算。
- **要量的指標**：主人的 `first_speak_ms`（尤其是外部回合已經開口、主人要等它說完的情況）、被切斷或退回的回合浪費的 token。

---

## §6 搶先與打斷

**誰能打斷誰**：
- 只有 **home 的即時輸入**（本機文字、語音 ASR final、IPC `Interrupt`、之後身體的即時語音）能搶先。主人的私訊是非同步文字，不搶先，Scheduler 一空下來就先輪到它（§5）；主人在公開頻道發言也不搶先。
- 被搶先的回合在私人領域（home、dm:owner、inner）：照下面的取消規則切斷。
- 被搶先的回合在外部 place：還沒有任何 E、而且這批輸入沒被退回過，就退回 inbox 最前面；否則讓它跑完，主人等這一個回合。
- **restart-before-speech**：同一個外部 place 有新訊息進來，正在跑的回合還沒有 E、而且這批輸入沒被退回過，就退回，讓下一個回合一次看到全部訊息。這修掉了 R2 的「過時回覆疊在一起」（`mvp-r2-findings.md:27-40`）。
- 「退回過」記在這批輸入最舊的那一筆上，上面兩條共用。所以同一批輸入最多被退回一次：別人的回覆不會遺失、不會被切斷，最多延後一次；一直傳訊息的人也不能讓她永遠不回。
- inner 的 trigger 不能搶先，inner 回合之間也不互相搶先。critical pulse 會等目前這個 agenda 步驟做完（agenda 本來就很短）。
- 背景工作不搶先別人。home 的即時輸入照今天取消 consolidation/evolution（`kernel.py:801-802`、`:1064-1065`）；其他來源不再取消，同時消除「被盜的 Discord 帳號反覆取消」這種 DoS。

**取消點**：每個串流 chunk、每個 pass 的邊界、兩次工具呼叫之間。已經在執行的工具不會被中斷（靠 timeout 收斂）。

**唯一的取消規則**（搶先、模型錯誤、crash 重播都用這一條）：
- **這回合還沒有任何 E**（沒說出任何句子、沒執行有效果的工具）：輸入退回 place inbox 的最前面（agenda 例外，直接作廢），什麼都不 commit，也不扣 energy。think-only 的這種回合今天本來就不扣；只呼叫過唯讀工具（recall、read_output）的今天會扣（`mind_loop.py:876` 任何工具呼叫都算有產出），新規則下不扣，這是一個小的行為改變，浪費的 token 記在 §5 的指標裡。
- **已經有 E**：partial commit。發生過的就是發生過了：工具改過的狀態保留，已說出的句子寫進 transcript，用掉的 token 照扣 energy，輸入（包括 inner 的 trigger）算已消費，**不重跑**。同時在同一個 place 留一筆 **cut note**，由程式寫成描述句，內容有：原因（owner_spoke | stopped | error | brain_down | crash）、已說出的句子、已完成的工具（有 R 記錄的，附結果摘要）、開始了但不確定有沒有完成的工具、沒執行到的平行呼叫，以及距離現在多久。
- cut note 在 home、dm:owner 和外部 place 會喚醒；主人搶先時，它跟主人的新輸入一起進下一個回合，所以她不會盲目重答，也不會再派一次已經在跑的 job。在 inner 不喚醒，跟下一個 inner 回合一起帶；日記要不要 retry，由 commit 時既有的 I1 檢查（有沒有成功 write_diary）決定。原因是 `error` 的 cut note 一律不喚醒，所以確定性的 bug 不會造成迴圈。

**語音與播放**：
- home 的即時輸入一律先中止正在播放的語音（`abort_speak`），不管有沒有回合在跑。今天只有 cascade 還在跑時才 abort（`kernel.py:1008-1012`），但 TTS 一秒只唸幾個字，播放常常比回合活得久。
- `abort_speak` 回報還沒唸出來的句子，寫成 home 的一筆 note（「這幾句沒唸出來就被打斷了」），跟主人的新輸入一起帶，所以她知道主人沒聽到哪些。`E(said)` 仍然是「交給 sink」，只用來保證 crash 後不重說。
- v1 在 ASR final 搶先，這是新行為（今天語音不會搶先，`kernel.py:1062-1074`）。SayAborted 送到**被取消那個回合**的 surface，不是送給發起搶先的那條連線。VAD `utterance_start` 層級的 barge-in 和多人語音輪流延到 P3。
- IPC `Interrupt`（「停」）：中止播放。正在跑的回合如果在 home，或已經說過話（有 E(said)），就以 `stopped` 切斷，輸入直接消費（C 的 outcome 寫 `stopped`），她不會馬上又回答被叫停的事。沒說話的 inner 回合（例如日記）不受影響。

**turn-end**：每批輸入在**有結果時**（被消費或被丟棄）送出恰好一次 TurnEnd/TurnEndAddressed，零句、切斷、出錯都一樣。被退回 inbox 的回合不送。否則 MCP 連接器會提早回 `no_response`。

---

## §7 狀態歸屬：一個寫入者

- **Self 是唯一的寫入者**：MindState（mood、focus、open_loops/agenda、energy、JobTable、演化與 consolidation 的帳、計數器、`recent_*`、每個 place 的 safe_mode、`recent_ingress_ids`、`committed_turn_ids`、`last_recovery_at`），以及 `current_self.md`、`self_history.jsonl`、`self_evolution/pending.json`、記憶 note、transcript/action log。Self 的方法都是同步的、內部不 await。asyncio 是單執行緒，所以所有寫入天然序列化。
- **回合內的工具照舊同步修改**（透過 ctx，ctx 握的就是 Self 的狀態），**不做 typed-delta 改寫**。安全來自兩件事：(1) 每個會改共享狀態或寫檔案的工具都算有效果，dispatch 前先寫 E；(2) 有 E 的回合永遠不重跑（§8）。所以即使背景工作透過 Self 方法觸發的存檔剛好存到這個回合做到一半的狀態，那也是真實發生過的事，replay 不會重做。反過來，crash 時還沒存檔的回合內修改（mood、focus、agenda、scratchpad）會回滾，寫檔的工具（remember、write_diary、self_note revise）卻已經落盤；CutOff 依 E 的工具名稱標出哪些是改狀態的工具，讓她回頭看 `[Mind state]` 確認。
- **回合開頭和結尾的寫入**：
  - 今天在回合**開頭**寫的：`recent_perceptions` 的 append（`mind_loop.py:461`）、`last_user_at` 和 `user_turn_count`（`:481-482`）、主人的 transcript 行（`:483-494`）、世界事件的 action-log 行（`:503-512`）、safe_mode 清除（`:584-591`）。`last_user_at` 是 presence，改在 route 時寫。safe_mode 在回合開始時只在 Turn 上解除（這回合拿完整工具），commit 才存。其他都改到 commit 才寫；`user_turn_count` 依這回合消費的主人輸入加，所以被退回重跑的回合不會重複加（consolidation 的閘門用它，`consolidation.py:180`）。
  - 今天在**結尾**寫、但被取消的 cascade 返回後照樣會寫的：`iter_count`（`:1029`）、energy 扣除（`:876-914`）、`note_reply`（`:1044`），以及在 dispatch 當下寫的 `tool_stats`（`:1816`）。全部改成只在 commit 寫。
  - 所以沒有 E 的回合不留任何痕跡，重跑也不會重複。
- **外部回合能寫的範圍**由 harness 矩陣限制：mood、自己範圍的記憶，以及自己 place 的工作記憶。唯一會寫到她共享狀態的是 mood（emotion 有長度和字元限制，reason 只在當地顯示，§10），last-writer-wins，都是她真實的感受。
- **背景工作不持有可寫的 MindState**，只拿同步複製的快照：
  - Consolidation 只寫自己的產出（`consolidated/{date}.md` 和它的 FTS row），然後呼叫 `Self.note_consolidation_attempt/done(...)`。
  - Evolution keeper/skeptic 呼叫 `Self.submit_evolution(result, base_version)`。Self 維護一個 `self_version`，只在會跟演化結果衝突的寫入時加一：SelfRevision 採納、演化寫入。PinSelf 和 `surface_or_expire` 不加（新的 pin 是下一次演化的材料，不是衝突），所以她自己在反思，不會讓演化延後一整週。版本不符就拒絕，記一行明確的 `stale_evolution_result` log，等下個週期再來。版本相符就在同一次同步呼叫裡寫 `self_history`（先寫 log 再寫檔）和 `pending.json`。採納仍然只能經由 reflection 回合的 `self_note revise`，gate chain 不變。`EvolutionTrigger.__init__` 的帳（`evolution_trigger.py:116-123`）改成啟動時呼叫一次 `Self.note_evolution_attempt`。
  - Energy 回復從 `consolidation.py:156-171` 搬到 `Self.tick`，閘門不變。
  - Workflow worker 的 `SUB_TOOLS` 拿掉 `NoteMemory`（`tools.py:1209-1214`）。worker 是她的工具，發現的東西寫在 Report 裡，要不要記住由她決定（行為改變）。Observer 本來就只讀狀態。
- **測試強制**：只有 `Self.save` 會呼叫 `save_state`（grep 測試）；背景工作改快照不會影響正本。

---

## §8 耐久性：turn journal

**記錄**（延用既有 WAL 檔，同一個 append 路徑）：

| 記錄 | 什麼時候寫 | fsync |
|---|---|---|
| `P {seq, place, principal, kind, t, data, ext_id, attempts}` | 入佇列時，settle 窗內的訊息也算 | 否 |
| `B {turn, place, kind, principal, seqs}` | 回合第一次呼叫 LLM 之前 | 否 |
| `E {turn, eff, type: said\|tool\|job, summary}` | **效果發生之前**：句子交給 sink 之前、有效果的工具 dispatch 之前。`eff = "<turn>:<n>"`，job 直接用 eff 當 job id，結果帶 `cause=eff` | **是**（同時把前面的 P/B 一起落盤） |
| `R {eff, ok, summary}` | 效果完成之後 | 否 |
| `C {turn, outcome, consumed:[seq]}` | Self.commit 存檔成功之後 | 否 |
| `D {seq, reason}` | 明確丟棄：被取代的 trigger、過期、poison、MCP 呼叫已關閉 | 否 |

工具在 ToolRegistry 裡宣告 `effectful`，預設是 true。只有唯讀工具（`recall`、`read_output`、讀 skill）是 false。MCP 工具一律算有效果。

**Commit 順序**（`Self.commit` 同步執行，中間不 await）：
1. 用 `append_once(turn_id)` 寫 transcript 和 action log。只有私人領域的回合（principal ∈ {owner, internal}；dm:owner 依 Q1）寫對話行和動作行；外部回合只寫一行程式產生的**外出紀錄**（§10）。閘門寫成白名單，不寫成 `!= external_public`，因為陌生人私訊在本文是 external_dm。這是行為改變：今天 C1 只管 action log 的工具行（`mind_loop.py:1820`），她在任何地方說的話都會寫進 transcript（`:954-962`），主人在公開頻道的發言也會（`:476-494`）。
2. 套用回合結束時的狀態變更（§7）。
3. 把 `turn_id` 加進 `committed_turn_ids`（保留最近 64 個），`save_state`（同今天，tmp + replace）。
4. 寫 C。
5. 送出後續 perception（警示、diary retry）。
6. 送 turn-end（§6）。

存檔失敗算 Self 的例外：daemon 結束，由 supervisor 重啟後走 recovery。她照常說話、卻什麼都沒存，正是 no-fallback 不允許的靜默降級。

**啟動時的 replay**（在任何 producer 啟動之前）：

| journal 裡的狀態 | 處理 |
|---|---|
| B 有 C，或 turn_id 在 `committed_turn_ids` 裡 | 已完成；缺 C 就補寫 |
| B 沒有 C，也沒有 E | 什麼都沒離開 daemon：輸入退回原本的 place，重跑。嘗試次數 = journal 裡含這批 seq 的 B 有幾筆（壓縮時寫進 P 的 `attempts`）；到 3 次就寫 `D(poison)`，並在 home 留一行明確的 `poison_input` 狀態，所以會讓 process 死掉的輸入不會造成無限的 crash loop |
| B 沒有 C，但有 E | **永不重跑**：用 append_once 補寫已說的 transcript，寫 `C(crashed)`，在同一個 place 放一筆 CutOff（同 §6 cut note：有 R 的列為已完成，其他列為不確定）。喚醒規則同 §6 |
| JobTable 裡剩下的 job，加上沒 commit 的回合裡的 `E(job)` | 在 spawn 它的 place 放 JobLost（喚醒），寫明「daemon 在這個 job 開始之後重啟過」。process 死掉時 job 也跟著死了，這一點明說 |
| P 不在任何 B 裡 | 依 seq 放回各自的 place，標 `replayed`（不能搶先） |

- **recovery 迴圈**：如果上一次開機也是在 10 分鐘內以 recovery 結束（`last_recovery_at`），這次 recovery 產生的 CutOff/JobLost 都只當 note、不喚醒。例如她用 run_command 重啟 daemon，就不會一直「再試一次」。
- 最後在 inner 放一筆 Awoke，內容是描述句（離線多久、幾個地方有訊息在等、今天的排程是否空白；所以開機時不另外發 `plan:<date>`），然後壓縮 journal。

**壓縮**：啟動時，以及檔案超過約 1 MB 時（在某次 commit 之後）用 tmp + `os.replace` 重寫，只留下還沒被 C/D 處理的 P（帶 `attempts`）和還開著的回合的 B/E/R。JobLost 從持久的 JobTable 推導，不靠 journal，所以壓縮不會讓還在跑的 job 消失。

**去重**：`ext_id` 會比對 `Self.recent_ingress_ids`（有上限、會持久化）和還活著的 P，重複的不寫入，只記 log。適用於：Discord msg_id、之後身體的 `client_event_id`、每日 producer 的固定 id（`diary:<date>`、`diary-retry:<date>`、`plan:<date>`、`sched:<date>:<entry>`）。這些 id 取代只存在記憶體裡的 fired-set、bootstrap 日期和 diary retry 日期（`kernel.py:716-719`、`mind_loop.py:266`）。

**例外隔離**：
- 一個回合丟例外，只影響這個回合自己的輸入；其他 place 的 inbox 完全不動。
- **傳輸層錯誤**（連不上、timeout、5xx，包括 llama-server 載入模型時的 503，以及串流到一半斷線；`transport.py:139-146`）：進入明確的 `brain_down`，不再開新回合，每 10s 用 `/health` 探測一次（這是 HTTP，不是 LLM 呼叫），回 200 才恢復。主人的 surface 收到 `BrainStatus(down, since)`。MCP place 立刻收到 `TurnEndAddressed(no_response, brain_down)`，輸入寫 D。Discord 和主人的輸入保留；恢復時，外部 place 裡超過 10 分鐘的輸入寫 `D(expired)` 並記 log，不會一次回一大堆舊訊息。被 brain_down 切斷、已經有 E 的 home/dm:owner 回合，恢復後它的 cut note 喚醒一次。
- **確定性錯誤**（4xx，例如 context 超長，以及其他例外）：沒有 E 時退回輸入、嘗試次數加一，第 3 次就寫 D，送 turn-end（no_response 或 failed）並記錯誤 log。有 E 時 partial commit，cut note 不喚醒。
- Scheduler 或 Self 本身出例外（包括存檔失敗）時，daemon 直接結束，由 supervisor 重啟後走 recovery，不再「continuing」（`mind_loop.py:364-370`）。

**明說的邊界**：
- 沒有 fsync 的記錄在 process crash 時是安全的；斷電時，最後一次 fsync 之後的記錄可能遺失（包括 R，所以斷電後的 CutOff 可能把完成的工具列為不確定）。macOS 的 `os.fsync` 不等於 `F_FULLFSYNC`。
- AttentionGate 拒絕的訊息不存。
- engagement session 只在記憶體裡，重啟後每個頻道都回到 cold。
- KV prompt cache 會失去。

---

## §9 她的內在生活

`inner` 是她的房間，裡面的回合是她生活本身，不是附在別人 batch 上的剩餘時間。主人來了，她會先放下手上的事，就像一個人放下日記去應門，但她的生活不靠主人存在。陌生人的對話跟她的房間輪流，不會把她排到最後。

**一個 presence 定義**，所有閘門共用：
- `owner_last_seen`：主人在任何地方的輸入都算，包括公開頻道（presence 和權限是兩個獨立的軸，同今天 `mind_loop.py:476-482`）。在 route 時寫。
- `owner_engaged`：120s 內看過主人。
- 是否有待處理的主人輸入（home 或 dm:owner）。

| 內在活動 | 觸發（observer 是薄的 producer，閘門是純函式） | 開始前再檢查 |
|---|---|---|
| **agenda**（興趣和好奇心就是 self_directed open loop，不加新機制） | AgendaObserver 四閘不變：主人閒置超過 600s、energy > 0.5、有 self loop、420s 節流（閒置今天就只看主人，`agenda_observer.py:68`） | 同樣四閘，不成立寫 D |
| **reflection** | 距上次已經有 ≥ 30 個**私人領域**回合（陌生人不再推動她的成長，今天是任何回合都算：`reflection_observer.py:38-52`）**而且**距上次 ≥ 30 分鐘。計數由 commit 持久化。文字改成邀請句，不再是「現在就用 PinSelf…」 | 主人不在 engaged 狀態，不成立寫 D |
| **diary** | 23:00（`diary:<date>`，trigger 帶自己的日期）。啟動時如果在 23:00–04:00 之間而且還沒寫，就補觸發。過了 04:00 還沒寫，action log 記一行明確的 `diary_missed:<date>` | 主人不在 engaged 狀態；不成立就保持 pending（§5） |
| **pulse** | `evaluate_alerts` 不變（含 900s 節流、延後不吞掉） | 無 |
| **event** | Awoke（開機，含今天的排程是否空白）、執行中換日（`plan:<date>`，描述句「今天還沒有排程」，取代每次連線就檢查的 bootstrap）。她自己的排程和 job 結果是 follow-up（§5） | 無 |

- **日記**：永遠不會再被併批，所以 `[Today's log]`、工具收窄、不說話、不寫 Doll transcript 每次都成立。這些都由 `kind == diary` 一個條件帶出，符合 diary spec「一個旗標」的要求。`[Today's log]` 的讀取和 write_diary 的檔名都用 trigger 帶的日期，不用 `date.today()`（今天在 `mind_loop.py:1143`、`tools.py:169`），所以過了午夜才寫的日記仍然看得到那一天的 log，也掛在那一天。回合後的 I1 檢查在 commit 時做：沒有 write_diary，就留下 marker 並送一次 `diary-retry:<date>`。
- **睡眠與 consolidation**：v1（Mac、沒有電池）沒有睡眠狀態機。Consolidation 和 evolution 是她的「潛意識」，主人閒置時在旁邊跑：consolidation 閒置 ≥ 300s，evolution ≥ 600s，冷卻時間不變。閒置的基準改成只看主人，所以她自己的 agenda 回合不再延後 consolidation。home 的即時輸入照今天取消它們（§6）。
- **之後的掛鉤**：iPhone 的充電和就寢以非喚醒的 `BodySensed` 進 home，由 `Self.tick` 讀取（充電 = 進食，恢復 energy）。loop 本身不用改。
- **energy**：代謝 v1 不變。每個回合在 commit 時扣：這回合自己的 token（(completion + 0.25·prompt)/unit）× GPU 熱乘數，以 turn_id 記帳。只有 home、dm:owner、inner 的回合扣（同今天：主人和 internal 扣）；外部回合都不扣，包括陌生人私訊（今天也不扣，因為它們是 external_public）。閘門寫成白名單（`mind_loop.py:877` 今天是 `!= external_public`），陌生人才不能用私訊把她的 energy 壓到 0.5 以下、關掉她的 agenda。「有產出才扣」照原文保留，唯一的小改變見 §6（沒有 E 就被退回的回合不扣）。`flat_legacy` 仍是唯一有標記的例外。agenda 的 0.5 下限仍是唯一的行為閘門，energy 永遠不擋反應回合。不加任何疲勞句子。
- **身體訊號**：pulse 和 `[Body signal]` 不變。BridgeDown/McpDown 改成身體狀態的一行（§4.1）。

---

## §10 Principal 與地方之間的隱私

- **principal 由 place 固定**：home = owner；inner = internal；`dm:<user>` = external_dm；公開頻道和 MCP = external_public；`dm:owner` = `PRINCIPAL_OWNER_DM`（§17 Q1）。不同 principal 的輸入天生在不同的 place，所以 harness 的「取最低信任」規則簡化成「place 的 principal，再被 taint 往下壓」。min() 仍然保留，當作斷言（縱深防禦）。
- **taint**：回合中途 recall `include_external` 或呼叫 `untrusted_output` 的工具時，會降低 `Turn.principal`，ToolRegistry 當場重新綁定工具。這只存在於 Turn 上，不會留在 Scheduler、Place 或 MindCtx。
- **延遲觸發**：job 記錄 spawn 當下（taint 之後）的 principal；schedule 記錄建立者的 place 和 principal；agenda loop 的 provenance 記錄建立者。後續回合的 principal 永遠不會高於它的來源（harness §6.5）。
- **`dm:owner`**：回覆一定回到 Discord 那條私訊。它跟 home 的關係依 Q1：(a) 完全併進私人領域；(b) 是比 owner 低一階的 `owner_dm`，看得到 home/inner，但寫的東西進不了主人的回合。

**可見性**（由 ContextBuilder 依 `recent_*` 上的 place/principal 標籤過濾）：

| 回合在 | 對話內容 | mood | focus／agenda／`[Self pulse]`（含視窗標題）／jobs |
|---|---|---|---|
| home | home，加上 inner 的尾巴；dm:owner 的尾巴只在 Q1(a) | 完整。外部（Q1(b) 時也包括 dm:owner）造成的 mood 只顯示 emotion，加註「（來自別處的對話）」 | 有 |
| inner | inner，加上 home 的尾巴；dm:owner 同上 | 同上 | 有 |
| dm:owner | dm:owner，加上 home、inner 的尾巴 | (a) 同 home；(b) emotion，reason 只在私人領域或這裡設定時才顯示 | (a) 有；(b) 依 Q1 的工具，不給 `[Self pulse]` |
| 外部 place X | 只有 X | emotion；reason 只在 X 設定時才顯示 | 沒有 |

- **mood** 只有一份 `{emotion, reason, set_place, set_principal}`。`emotion` 在 pydantic 層限制為 ≤ 6 個字、只能是文字，不能有空白和標點；今天這只寫在 description 裡（`tools.py:855`）。所以外部回合就算被注入，也只能把一個短詞放進主人的 `[Mind state]`。這是本文對 harness Q1 的回答（§17 Q3）。
- **外出紀錄**：外部回合（Q1(b) 時也包括 dm:owner）commit 時，由程式在 action log 寫一行不含原文的描述句，例如「在一個公開頻道聊了 4 則，心情變成 煩」：只有 place 種類、則數和 emotion 的變化，沒有對方的文字，也沒有對方能控制的名稱。日記的 `[Today's log]` 看得到，所以她在外面的生活會進日記；consolidation 依 I2 本來就濾掉 action 行。
- 記憶自動注入依 harness S1，外部目錄不注入私人回合。`[Memory context]` 的查詢改用這回合自己的輸入。
- 外部原文進入私人回合的唯一路徑是 `recall include_external`，讀了就降權。
- safe_mode 和 RepeatLoop/PersonaDrift 警示都以 place 為單位：
  - 私人 place 的 safe_mode 在該 place 下一次有主人輸入時解除；inner 的則是 home（Q1(a) 時也包括 dm:owner）任何一次主人輸入都會解除。
  - 外部 place 的 safe_mode 在它的 engagement session 過期時解除。
- **輸出路由**：
  - home 的回覆：這批輸入的 surface 還在就用它；不在（例如 CutOff、JobLost、排程結果，或重啟後原本的連線已經不在）就送到最近活躍的主人 surface；都沒有就在 home 記一行 undelivered。
  - inner 的發話（pulse、event）送到最近活躍的主人 surface；沒有本機主人 surface 時，送到最近活躍的 dm:owner（`AddressedText`）；都沒有就在 home 記 undelivered（之後改成 APNs）。
  - 外部 place 和 dm:owner 用 `AddressedText(channel_id)`。surface 不在（BridgeDown、MCP 呼叫已關閉）時不開新回合，輸入保留（過期規則同 brain_down）；已經在跑的回合，輸出記成 undelivered，**絕不改送到別的 sink**。今天的 `sink_resolver.py:57-67` 會退回最近的 internal sink，也就是可能把給陌生人的回覆送到主人的 client。

---

## §11 跟 harness 的邊界

**本文取代 harness §2 圖上的「Perception queue（既有，不變）」**，改成 `route()` + Journal + Place inbox + Scheduler。Turn Runner 第 1 步「算出 principal 和 TurnPolicy」改由 loop 交給它（`turn.kind`、`turn.principal`）；runner 仍會算 min(inputs) 作為斷言，並負責 taint。

**介面**：`TurnRunner.run(turn) -> TurnResult{outcome: done|cut|failed|brain_down, said, effects, usage, tool_outcomes}`。Turn 帶著 `id, place, kind, principal, inputs, notes, surface, cancel, journal_effect()`。

**在 harness 已核可的元件裡要加的小東西**（形狀不變）：

| harness 元件 | 加什麼 |
|---|---|
| ToolRegistry（S5） | 每個工具宣告 `effectful`（預設 true） |
| ToolExecutor（S5） | effectful 的工具 dispatch 前先 `journal_effect`、完成後寫 R；job 用 eff 當 id 並登記到 JobTable（place、spawn 當下的 principal、turn_id）；兩次呼叫之間檢查 cancel；記錄 not_run |
| ModelAdapter（S4/S6） | 每次呼叫可取消；傳輸層錯誤回 `brain_down`（§8） |
| ContextBuilder（S5） | 吃依 place 過濾過的 view（§10）和 cut note |
| sink flush | 句子交給 sink 前先寫 `E(said)` |
| `mood` 工具 | `emotion` 限長度和字元（§10） |
| TurnPolicy（S3） | 依 §4.3 的 kind 查表 |

- 背景 agent（workflow worker、consolidation keeper、evolution keeper/skeptic）用同一個 Turn Runner，拿快照跑。它們不是 place，也不另外寫 B/C；job 的 E 記錄在 spawn 它的那個回合裡。這符合「Turn Runner 只有一套」。
- harness §6.5 說「drain_grouped 已經按頻道分組，大致成立；定成規則並加測試」，改由 place 和 route 表格測試達成。
- harness §9 S9 的「排程與 agenda 記錄建立者 principal」提前到 M1（路由需要它）。
- harness §6（矩陣是唯一的權限來源）不變。唯一的例外是 Q1 選 (b) 時，矩陣加一欄 `owner_dm`。
- **harness §7 需要修正（M6 需要，§17 Q2）**：「perception 佇列、WAL、單一消費者的 mind loop」改成「perception 入口加上每個 place 的 inbox；WAL 擴成 turn journal；一個 Scheduler 消費、一次一個回合；一個寫入者（Self）」。「打斷機制」改成「只有 home 的即時輸入能打斷；外部回合最多被延後一次，不會被切斷」。M1–M5 不需要這個修正。

---

## §12 不變式（都要有測試）

1. 每個 perception 入佇列時就帶著 place 和 principal；loop 不推斷這兩樣。
2. 一個回合只有一個 place、一種 kind、一個 effective principal。inner 每回合恰好一個 trigger，每種 kind 最多一個 pending；同一個來源的 follow-up 在一個 place 最多一個 pending。
3. 同時最多一個回合。
4. 只有 home 的即時輸入能搶先。私人領域的回合會被切斷；外部回合只在還沒有 E 時被退回，同一批輸入最多退回一次（restart-before-speech 共用這個上限）。別人的回覆不會遺失、不會被切斷，最多被延後一次。
5. 只有 `Self.save` 會呼叫 `save_state`；Self 的方法裡沒有 await；背景工作不持有可寫的 MindState。
6. 每個效果在執行之前就寫好 E 並 fsync；有 E 的回合永不重跑，inner 也一樣。
7. 每筆 P 最後都恰好被一個 C 消費，或有一筆 D。沒有靜默遺失。
8. 每批輸入的 turn-end 恰好送一次。
9. 外部回合看不到其他 place 的內容；私人回合看不到外部原文，除非透過會降權的 recall。
10. principal 只會降不會升；job、schedule、agenda 這些後續觸發都不高於它的來源。
11. 只有私人領域的回合寫 transcript 的對話行和動作行；外部回合只留一行不含原文的外出紀錄，而且不扣 energy。

---

## §13 遷移

一次只換一塊，每一步測試都要全綠。

| 步驟 | 內容 | 修什麼 | 行為改變 | 依賴 |
|---|---|---|---|---|
| （S1，進行中） | 外部記憶隔離 | — | — | — |
| **M1** | Perception 加上 `place/principal/wakes/preempts/ext_id`；`route()` 表加表格測試；各 producer 蓋章；最小的 JobTable（存進 MindState）；WriteSchedule 記錄建立者；所有 ScheduledMoment 從 `intent` 渲染；`drain_grouped` 改成依 (place, kind, effective principal) 分組，inner 每批一個 trigger | 問題 1、排程空白 | 日記/agenda/reflection 不再併批；agenda 不再併進主人的 batch（推翻 agenda spec §4.2）；主人本機 = owner；排程內容第一次出現在 prompt 裡 | S1 merge 之後；跟 S2 一起做（S2 的 min() 變成斷言）；先決定 Q1 |
| **M2** | Turn 物件 + ContextVar（MindCtx 保留相容屬性）；所有旗標改成看 `turn.kind`；TurnPolicy 表 | 問題 4（旗標） | 無 | = harness S3 |
| **M3** | Journal P/B/E/R/C/D、每回合各自 C、replay（CutOff/JobLost、attempts 與 poison、recovery 迴圈保護）、例外隔離、`ext_id` 與每日 id、diary 補觸發；transcript 白名單 + 外出紀錄；刪掉 `truncate_through` | 問題 3 | crash 之後她看到 CutOff，不再重跑；外部回合不再寫 transcript，她在外面的生活只以外出紀錄進日記；主人在公開頻道的發言不再寫進 transcript | M2 |
| **M4** | Self 成為唯一寫入者；背景工作改用快照 + Self 方法；`Self.tick`；`submit_evolution`；`SUB_TOOLS` 拿掉 NoteMemory；save_state 呼叫位置的 grep 測試 | 問題 5 | worker 不再寫記憶 | M3 |
| **M5** | 內在生活：reflection 的計數、安靜閘門、邀請句、不說話；開機 Awoke + 執行中換日 event 取代 bootstrap；diary 帶自己的日期、閘門不成立時保持 pending；BridgeDown/McpDown 改成狀態 | 定位 | 是（Q4） | M2 |
| — | harness S4、S5（ModelAdapter、單一 Turn Runner、刪掉 `cascade/tool_loop`） | 問題 4（兩套 cascade） | 無 | — |
| **M6** | Place inbox + `ready_at`（刪掉 BatchAccumulator）；`recent_*` 加 place/principal/seq 標籤；§10 可見性（含 mood reason、focus、emotion 限制）；sink 角色 + token + Origin 檢查；每個 place 的 safe_mode；搶先只限 home、外部回合最多退回一次；取消規則和 cut note；語音播放中止與沒唸出來的句子；restart-before-speech；follow-up 合併與上限；`brain_down` | 問題 2（主人不再取消別人的回合，最多等一個已經開口的外部回合）＋ §1 的洩漏 | 是：主人私訊和公開頻道發言不再搶先、不再取消 consolidation | S5；Q2 核可 |
| — | 兩條並行 lane | 量了 M6 的 `first_speak_ms` 再決定，另寫 spec | | |
| — | harness S6–S9 | | | |

M1、M4、M6 之後各做一次 live smoke。M6 的 smoke 包括：主人在公開頻道和私訊發言都不搶先；外部回合已經開口時，主人的 `first_speak_ms`。

---

## §14 測試計劃

- **路由**：§4.1 的每一列都寫成表格測試，包括 fail-closed 的 provenance 和 `legacy_provenance`。加一列：`dm:<user>` 的回合 commit 之後，`transcripts/` 只多一行外出紀錄，energy 不變。
- **渲染**：用真實 producer 的 payload（排程器、bootstrap 的 `intent`）測 ScheduledMoment 的渲染，不用手寫的 `text`。
- **批次**：日記不跟 agenda/reflection 併批；被汙染的結果自己跑一個回合；inner 每種 kind 只保留一個 pending；同一個 monitor 的結果在一個 place 只留一個 pending；follow-up 超過上限變成 note；主人的多則訊息合成一個回合。
- **TurnPolicy**：每種 kind × principal 的工具集合，沿用 harness §9.1 的權限測試。
- **Journal crash 注入**：在每個邊界殺掉 process：P 之後、B 之後、E 寫入後但 dispatch 前、效果發生後但 C 之前、save 和 C 之間、壓縮中途。property 測試：每筆 P 恰好被一個 C 消費，或有一筆 D；重啟前後 `E(said)` 和 `E(job)` 都不會重複；有 E 的 inner 回合（例如已經 write_diary 的日記）不會重跑。
- **Replay**：CutOff 的內容（有 R 的列已完成，其他列不確定）、壓縮之後再 crash 仍然產生 JobLost、`committed_turn_ids` 補洞、`ext_id` 去重（bridge backfill、outbox 重送）、同一批輸入第 3 次 crash 寫 `D(poison)`、10 分鐘內連續 recovery 時 CutOff/JobLost 不喚醒。
- **brain_down**：503 `Loading model`、read timeout、串流中斷都進 brain_down 並等 `/health` 200；4xx 才算進 3 次；恢復時超過 10 分鐘的外部輸入寫 `D(expired)`。
- **單一寫入者**：`save_state` 只出現在 Self 裡；改快照不影響正本；`submit_evolution` 在 self_version 不符時拒絕，PinSelf 不會讓它不符。
- **搶先**：home 輸入會切斷私人領域的回合；外部回合沒有 E 時退回、有 E 時跑完；同一批外部輸入最多退回一次（陌生人每 3 秒傳一則，她仍然會回）；主人私訊和公開頻道發言不搶先；cut note 的欄位；SayAborted 送到被取消回合的 surface；沒有回合在跑時 home 輸入仍會中止播放，沒唸出來的句子進 note；Interrupt 不影響沒說話的 inner 回合。
- **日記**：00:30 才跑的日記讀前一天的 log、寫前一天的檔；23:00 主人正在聊天時日記保持 pending，聊完才跑。
- **隱私**：外部回合的 prompt 裡沒有 home 的文字、focus、其他 place 的內容；私人回合的 prompt 裡沒有外部原文；mood reason 的可見範圍；emotion 超過 6 個字或帶標點時被拒絕；Q1(b) 時，被盜帳號情境下 home 回合的 prompt 裡沒有任何 dm:owner 寫入的文字。
- **sink**：沒帶 token 或帶瀏覽器 `Origin` 的連線被拒；bridge 比本機 client 晚連上，也搶不走主人的輸出；沒有主人 surface 時記 undelivered；bridge 斷線時給陌生人的回覆不會送到主人的 client。
- **live smoke**：23:00 時 agenda 正在跑，日記仍然正常；不說話的 reflection 仍會用 PinSelf/SelfRevision；主人和 Discord 同時說話時量 `first_speak_ms`。

---

## §15 不做的事

- 不加 reflex 層、第二個或小模型、SNN；不用數值 salience 分數。所有決定都是 daemon 裡的規則。
- v1 不做睡眠狀態機、不做夢、不做 nap。energy 不加新的行為閘門。
- 不加 `[Elsewhere]`／periphery 區塊，也不加任何新的 prompt 指令或疲勞句子。只有三種由程式寫的描述句：cut note、醒來通知、外出紀錄。
- 不開並行 lane，不加 slot 保留（量過 `first_speak_ms` 再說）；inner 回合之間不互相搶先。
- 不做非同步的 MCP job（例如 VMPal 的慢呼叫），留給 VMPal 自己的 spec 或 harness S7。
- 不把所有工具改寫成 typed delta。
- 不改 energy 的計算（think-only 免費、外部回合不扣）。不改 harness §6.3 矩陣，唯一的例外是 Q1(b) 加一欄。
- 不做 VAD 層級的 barge-in，也不做多人語音輪流（延到 P3）。
- 不讓 job 在 daemon 重啟後存活（會明說遺失）；不存 AttentionGate 拒絕的訊息；不補跑錯過的排程。
- 不做主動外聯（B 仍然延後）。

---

## §16 風險

- **`-np 2` 會把 n_ctx 切給每個 slot**：日記約 40k 字的 `[Today's log]` 必須塞得進單一 slot。S4 要確認 server 的設定。
- **主人私訊的權限**：見 Q1。
- **陌生人會影響她的 mood**（emotion 只是一個短詞，理由看不到），持續惹她，可能影響她面對主人時的狀態（Q3）。
- **主人要等已經開口的外部回合**：外部回合的工具很少，通常幾秒。M6 量 `first_speak_ms`，不能接受再談並行 lane。
- **飢餓變得明顯**：主人聊很久，她的內在生活就會延後（日記保持 pending 到 04:00）；外部 place 排隊時，MCP `talk()` 可能撞上 60s timeout，這會被記錄。
- **「effectful」的宣告必須正確**：任何繞過兩個寫入點（sink flush、ToolExecutor）的新副作用，重播時都可能重複。所以預設 effectful，另外用測試檢查。
- CutOff 把決定交給她，她仍然可能在 crash 之後重做一個不冪等的動作。
- restart-before-speech 和主人造成的退回會多花一次 prefill（每批最多一次）。外部 place 的 CutOff 可能造成遲到的回覆。
- journal、壓縮、replay 是新的關鍵程式碼，需要 crash 注入和 property 測試。
- **有行為改變，需要 live smoke**：reflection 不說話也沒有 Shell；agenda 不再併批；主人本機 = owner；語音會搶先；worker 不再寫記憶；主人私訊和公開頻道發言不再搶先、不再取消 consolidation；外部回合不寫 transcript（改成外出紀錄），主人在公開頻道的話也不再進 transcript；只呼叫唯讀工具就被退回的回合不扣 energy；排程內容第一次出現在 prompt 裡；連線要帶 owner token。

---

## §17 待決問題

1. **Q1 主人在 Discord 私訊（`dm:owner`）的權限**。這不只是改一個常數：home 和 dm:owner 都是 conversation 回合，兩案的差別要靠 principal 表達。
   - (a) `owner`：harness §6.3 的寫法。dm:owner 完全併進私人領域：有 run_command，跟 home/inner 互相看得到，寫 transcript，算進 reflection 的計數，也能解除 inner 的 safe_mode。最符合「同一段關係」，Mac 先上、主人主要用私訊時尤其明顯。代價：Discord 帳號被盜，就等於家裡電腦被遠端執行指令，也能在她的記憶和自我裡留下持久的指令。
   - (b) 新的 principal `owner_dm`（矩陣加一欄，§15 唯一的例外）：工具只有說話、recall（主人範圍，唯讀）、mood、learn_name、remember（寫到 `external_dm/owner/`，依 harness S1 不自動注入私人回合）。它看得到 home/inner 的尾巴，inner 的發話也能送到這裡；但 home/inner 看不到它的原文，transcript 只留外出紀錄，也不算進 reflection 的計數。被盜的帳號讀得到她的私事，但寫不進主人的回合。代價：私訊裡的對話只以一行紀錄進日記；主人主要用私訊時，她的內在生活和這段關係很少交集。這跟今天的 code 最接近（今天私訊只有 `EXTERNAL_TOOLS` + LearnName，NoteMemory 寫到 `external_dm/`）。

   **建議 (b)**，等 harness §10 的主人確認機制上線後再升成 (a)；如果你主要會用私訊跟她說話，選 (a)。M1 之前要決定。
2. **Q2 核可 M6 需要的 harness §7 修正嗎？**（§11）perception 佇列換成 place inbox + Scheduler；打斷只限 home 的即時輸入，外部回合最多被延後一次、不會被切斷。不核可的話，M1–M5 仍然會修好問題 1、3、4、5，但主人開口仍會取消別人的回合、弄丟別人的回覆。兩條並行 lane 不在這次範圍。
3. **Q3 外面的事怎麼進到她的私人生活**（這是對 harness Q1 的回答）。今天陌生人不能動她的 mood（`EXTERNAL_TOOLS` 沒有 MoodTool）；照 harness 矩陣開放之後，陌生人可以改她唯一的 mood，但 emotion 只能是一個短詞，理由只在當地看得到。她在外面的生活以一行不含原文的外出紀錄進日記。可以嗎？另一個選擇是外部回合的 mood 只在那個 place 裡生效。
4. **Q4 reflection 的改變**：不說話（日記才是內在生活的出口）、只算私人領域的回合、只在安靜時觸發、用邀請句。可以嗎？
