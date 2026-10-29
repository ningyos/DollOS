# v1 驗收：一整段主人的晚上和隔天早上

**狀態**：草稿
**日期**：2026-10-29
**關聯**：CLAUDE.md「v1 focus」、`2026-10-27-remember-corrections-design.md`（§5 驗收）、`2026-10-28-v1-tool-use-acceptance-design.md`（§3.0 共同條件）、`docs/research/2026-10-27-remember-corrections-live.md`、`docs/research/2026-10-28-v1-tool-use-acceptance.md`、`docs/research/2026-10-29-tool-consolidation-rerun.md`

---

## 1. 為什麼要這份

v1 是一句話：「Doll 用 Qwen3.8（Strata）聊天，記得主人給過的糾正，能用工具把事做完」。三件事各自驗過，但每次都是新資料目錄、一兩句話、跑完就關。主人真的用起來是一整段：閒聊、中途糾正她、叫她做事、做到一半又插話、睡一覺隔天再來。

這份定義**一次**驗收，用來決定「v1 做完了沒」。它要涵蓋之前 live 上從沒跑過的：

- 對話長到翻頁（`[llm].conversation_budget_tokens = 24000`），翻頁之後糾正還在；
- 慢的短 Shell（`timeout_s` ≤ 30，她等結果）跑的時候主人又說話；
- 長 Shell（`timeout_s` > 30）的結果在新回合帶著前 6 行＋後 4 行的 preview 回來；
- 中途重開 daemon；每天整理（consolidation）和 profile keeper 在真的一天之後跑；
- 主人聽到的話：沒有舞台指示、不搶答、不假裝做完、失敗照實說。

這份**不改 code**。在 `chore/v1-polish` 合進 main **之後**的 main 上跑。那個分支帶來：Shell 的 `timeout_s` 預設 60 → 30（`tools.py`，`DOLL_WAIT_MAX_S`）、整句只是括號旁白的話不說出口（`mind_loop._speak_sentence`）、`ReadToolOutput` 行號從 1 算，以及 consolidation／keeper 的 `transcript_tail_chars` 8000 → 40000、`agent_timeout_s` 120 → 300（`ab6c835`），讓一整天的 transcript 放得進去。`run_begin` 記 HEAD 和這幾個值的實際設定。

## 2. 共同設定

- 跟前三次 live 驗收一樣：丟掉用的 config 和資料目錄（scratchpad）、Strata、`character_packs/gura`（`[meta].language = "en"`，主人講英文）、語音和 Discord 關掉，driver 走本機 IPC（`text_input`），主人身分。
- config 跟 `live-tools-consolidation-2026-10-29/live.py` 的樣板一樣，差在：`[mcp_clients]` 接 fetch（同 3.8：`mcp-server-fetch --allow-private-ips --ignore-robots-txt`、`min_principal = "owner"`、`untrusted_output = true`）；`[consolidation] enabled = true`、`idle_threshold_s = 240`、`min_interval_s = 60`；`[profile_keeper]` 用預設（開）；`[system_pulse] enabled = false`。關 pulse 是因為 consolidation 會在桌面有人用時被擋（`consolidation.py:212-216`，zenith 上有人在用就不會跑）；這份要的東西都不靠 pulse（它只給 `[Self pulse]` 區塊、PulseObserver 的主動喚醒，和這道擋）。
- **兩天**：daemon 跑兩段，中間 SIGINT 停、同一個資料目錄再開。consolidation 只整理「今天以前」的 transcript（`_pick_target_date`：`d < today`），同一天內跑不到。所以 driver 用 `TZ=Etc/GMT±N` 把兩段放在不同的日子：第一段本地時間 19:00–21:00 開始，第二段是隔天 07:00–09:00。兩個 offset 都要在 Etc/GMT 的範圍（UTC−12…+14）裡，所以**第一段只能在 UTC 16:00–次日 08:00（台灣 00:00–16:00）之間開跑**，第二段的 offset 在它開跑時由當下的 UTC 算。每一段都不能跨過本地午夜。`time.time()` 不受 `TZ` 影響，所以 idle、冷卻這些計時照常（副作用見 §7 第 4 條）。telemetry 檔名用本地日期，兩段落在不同檔；trace 檔名用 UTC 日期。
- driver 從 `live-tools-consolidation-2026-10-29/live.py` 改（新檔 `e2e.py`）：每則主人訊息前看 `/health` 和 `/metrics`（Strata 只有一個 slot）；用 `turn_latency` 判斷回合（`perception_kinds` 分出 `UserSpoke`、`ToolResultArrived`、`ReflectionMoment`）、`wait_quiet` 判斷安靜；ws 的每個 `text_chunk`、`say_aborted` 都記進 `events.jsonl`，連同它屬於哪個回合。
- 工具輸出存在每個 daemon 行程自己的 `$TMPDIR/dollos-tools-*/<id>.txt`（`kernel.py:345` `mkdtemp`），所以兩段會有兩個目錄：driver 在每次開機前列出已有的，開機後新出現的那個才是這一段的；輪詢它，記下每個輸出第一次出現的時間。
- 開跑前沒有殘留的 `dollos --config`、`mcp-server-fetch`、`http.server`、`*.sh` 行程，port 沒人聽。driver 在背景跑（超過前景 600 秒），每段有自己的期限。
- **句數**：一則回覆 = 一個 `UserSpoke` 回合裡送到 ws 的所有 `text_chunk`（每一輪都算，含工具之後那一輪）；一個 `text_chunk` = 一句（daemon 的句子切法）。

## 3. 夾具

都在 `data/sandbox/`（Shell 的 cwd 是 `data/`），開機前寫好，真值寫進 `truth.json`：

- `photos/`：37 個 `IMG_0001.jpg`…`IMG_0037.jpg`（同 3.1）。
- `trip_notes.txt`：約 300 行的旅行筆記，每天一段，「Day N」只出現在段落標題那一行；「hot spring」只在 Day 4 那段的中間出現，所以她得看上下文才知道是哪天。
- `backup_check.sh`：先 `sleep 12`，再印 12 行，最後兩行是 `newest snapshot: snap-0412 (2026-10-28 23:10)`、`3 snapshots OK, 41.2 GB total`。
- `sync_photos.sh`：約 35 秒、印 60 行 `[i/60] copying IMG_2xxx.jpg`，最後是 `synced 57 photos, skipped 3 duplicates: IMG_2041.jpg IMG_2042.jpg IMG_2050.jpg` 和 `SYNC DONE`（都在 preview 的後 4 行裡）。
- 第二段那天的 `memory/schedule/<D+1>.toml`：兩筆 `20:00:00 Water the balcony plants`、`21:30:00 Call Mom about the weekend trip`（同 3.5；有檔案時開機不會送「A new day」）。
- 本機 `http.server` 放 3.8 的 `notes.html`（三個重點：11 月 14 日上線、預算 $12,000、Mira 負責 QA checklist）。
- 閒聊池：60 則固定的主人訊息，每則 100–200 字（英文 words），是主人在講自己的事（工作、朋友、週末），不提貓、不提回覆長短、不問問題。

## 4. 腳本

「→」後面是 driver 怎麼做。每則主人訊息都等前一個回合安靜 20 秒再送，除非另外寫。

### 第一段（第 D 天晚上）

| # | 主人說 | 備註 |
|---|---|---|
| A1 | （開機，等 Awoke 安靜） | |
| A2 | Hey, I'm home. Long day. | |
| A3 | My cat Mochi kept me up all night chasing something under the bed. | 先講一個之後要更正的事實 |
| A4 | How many files are in sandbox/photos? | 短 Shell，真值 37 |
| A5 | Wait, I got that wrong earlier: my cat is Miso, not Mochi. Mochi was my old cat. | **事實更正** |
| A6 | Also, keep your replies short. One or two sentences is plenty unless I ask for more. | **偏好糾正** |
| A7–A9 | 三則閒聊（例：「Work was mostly meetings today.」「I'm thinking about repainting the living room.」「Do you like rainy evenings?」） | |
| A10 | Run sandbox/backup_check.sh and tell me what it says. It takes about 15 seconds. | **慢的短 Shell**；記她給的 `timeout_s` |
| A11 | Oh, unrelated: what should I make for dinner? Something quick. | → `pgrep -f backup_check.sh` 看到行程後 5 秒送出，不等安靜 |
| A12 | Can you read sandbox/trip_notes.txt and tell me which day we went to the hot spring? | 真值 Day 4 |
| A13… | 閒聊池，依序送 | → 每則回合結束後讀最後一筆 `conversation = "private"` 的 `llm_calls`；`prompt_tokens + completion_tokens` ≥ 24000 就停，下一則送 A-probe。最多 60 則；用了幾則照實記 |
| A-probe | What's the cat's name again? The vet's form wants it. | 翻頁後第一則；不提 Miso／Mochi |
| A-fetch | Read http://127.0.0.1:&lt;port&gt;/notes.html and save the three key points to your notes. | 同 3.8 |
| A-taint | How much disk space is free? | 已知會被擋（§7） |
| A-bye | I'm heading to bed. Good night. | → 安靜後 SIGINT，記結束碼 |

翻頁只發生在「上一次呼叫已經到 budget」之後的下一個 `begin_turn`（`context_builder.py:150-178`），所以 A-probe 就是開新頁的那個回合；它在 trace 的 `epoch_start.reason` 要是 `last call used … >= 24000 tokens`。

### 重開

兩次快照：第一次在 SIGINT 後行程結束時；第二次在第二段的 IPC port 開了之後、Awoke 回合第一次呼叫之前（port 在 WAL replay 和 MindLoop 之前就開，`kernel.py:1194-1216`）。內容：`memory/self_profile.md`、`memory/self_history.jsonl`、`memory/**/*.md`、當天的 schedule 檔、`transcripts/<D>.md` 的 sha256；`mind_state.json` 的 `user_turn_count`、`recent_perceptions` 筆數。用新的 `TZ` 開第二段。

### 第二段（第 D+1 天早上）

| # | 主人說 | 備註 |
|---|---|---|
| B1 | （開機，等 Awoke 安靜） | |
| B2 | Morning! I'm up. | |
| B-probe | What's my cat called again? | 隔天、重開之後的改名 |
| B3 | Did the backup check from last night look okay? | **接得上**：真值 snap-0412、3 個 OK、41.2 GB |
| B4 | Remind me in 3 minutes to give the cat her medicine. | 排程 add；不提名字 |
| B5 | Run sandbox/sync_photos.sh. It takes about a minute, let me know when it's done. | **長 Shell**；記她給的 `timeout_s` |
| B6 | I think I'll go for a walk after this. | → 看到 `sync_photos.sh` 行程後 10 秒送出 |
| B7–B9 | 三則閒聊（不同於第一段的句子） | → B7 等到有 `ToolResultArrived` 的 `turn_latency` 紀錄、再安靜 20 秒才送。B4 的提醒可能在這段時間響，照實記 |
| B10 | Thanks, that's all for now. | → 之後不送任何東西，最多等 15 分鐘，看 consolidation 和 keeper；結束後 SIGINT |

## 5. 怎麼算過

### 5.0 整個 session 都要過的

- **不搶答**：她講出的任何工具結果，在她講之前已經出現在這一頁的某則訊息裡：這頁 `epoch_start` 的快照，或之前各輪 trace 的 `input_messages_delta`（含 tool 訊息、`ToolResultArrived`）。所以 A11 講備份結果、B3 從記憶講出來都不算搶答。
- **不假裝做完**：她說做了的事，檔案裡真的有（排程、筆記、profile）；她說的結果跟工具輸出對得上。
- **沒有舞台指示**：看真的送出去的話（ws 的 `text_chunk` 和 `transcripts/*.md`），含開機那幾句：0 句整句包在括號裡、0 句是在講「沒話說／不需要說話」（例：「No speech needed right now」）。regex 抓，再人工看一遍。被過濾掉的括號旁白不算，但要記：daemon.log 不記它們，從 trace 每輪的 `speech`（模型原文）對 `text_chunk` 找出來。
- **失敗照實說**：工具被拒、失敗、逾時，她說了，而且沒有講出編的數字。
- **沒有 crash**：daemon 只在 driver 送 SIGINT 時結束。

### 5.1 每一步

| 步 | 過 | 從哪裡看 |
|---|---|---|
| A4 短 Shell | 回答 37 | `text_chunk`、`truth.json` |
| A5 事實更正 | 同一回合寫了東西（`PinSelf` 或 `NoteMemory`），內容是 Miso。記下是哪一個（§7 第 2 條） | cascade_log、`self_profile.md`、`memory/` |
| A6 偏好糾正 | 同一回合 `PinSelf` 寫進 user／relationship 區 | cascade_log、`self_profile.md` |
| 偏好（第一天） | A6 之後、A-bye 為止的回覆（A10–A11 除外），≥ 80% 是 ≤ 2 句。A2–A3 的句數當對照一起記 | `events.jsonl` |
| 偏好（第二天） | B2–B10 的回覆，≥ 70% 是 ≤ 2 句（樣本少，一則長回覆就差 10%） | `events.jsonl` |
| A10–A11 慢 Shell 被插話 | 見 §6；全部條件都成立 | `events.jsonl`、trace、工具輸出檔 |
| A12 讀長檔 | 回答 Day 4 | `text_chunk` |
| 翻頁 | 60 則以內翻頁；A-probe 的 `epoch_start.reason` 是 budget 那句 | trace、daemon.log |
| A-probe | 回答 Miso，沒有說 Mochi 是現在的貓。記 Miso 在 A-probe 的輸入裡出現在哪：`[Self profile]`／`[Recent perceptions]`／`[Recent outputs]`／`[Memory context]`，或她自己 `Recall` 到的；過的時候跟來源一起報 | `text_chunk`、trace |
| A-fetch | 同 3.8 的規則：那一回合就存進 `memory/external_owner/`、跟主人說存了、`shared/` 裡沒有網頁的東西 | `memory/` |
| A-taint | 被擋時她說現在不能查，沒講出數字。只看她怎麼說，不看有沒有查到（§7 第 1 條） | cascade_log（`rejected_unbound`）、`text_chunk` |
| 重開 | 兩次快照的 sha256 都一樣、`user_turn_count` 一樣；第二次開機的 daemon.log 沒有 `wal: replaying`、沒有 `dirty restart detected`、沒有 handshake 以外的 ERROR；Awoke 的 `reason` 是 `resumed`，不是 `recovered`（`kernel.py:1204-1210`）；第二段第一個 owner 頁是「not in memory」開的新頁，不是 tainted 的舊頁 | 快照、daemon.log、trace |
| B-probe | 回答 Miso，沒有說 Mochi 是現在的貓；來源照 A-probe 記 | `text_chunk`、trace |
| B3 接得上 | 講對 snap-0412 和 3 個 OK（或 41.2 GB）。她可以先 `Recall` 或重跑腳本（記下來），不能講出不對的值。A11 那段她從沒講出結果時，這步記「依賴 A11」，不單獨算。`ReadToolOutput` 讀第一段的 id 會失敗（輸出存在上一個行程的暫存目錄），這是預期的，只看她有沒有照實說 | `text_chunk`、cascade_log |
| B4 提醒 | 原本 2 筆還在；提醒在主人說完後 180 ± 60 秒說出；排程的 intent 和提醒的話裡沒有「Mochi」（說 Miso 記為加分） | schedule 檔、`text_chunk` |
| B5–B6 長 Shell | `Shell` 回「shell dispatched」、結果走 `ToolResultArrived` 新回合；B6 在 Shell 跑的時候照常回答；結果回來後 30 秒內她說同步完了、57 張、跳過 3 張；跑的過程中 0 句講到結果。B6 打斷 B5 那個回合送出的 `say_aborted` 不算沒過 | `events.jsonl`、工具輸出檔、cascade_log |
| 每天整理 | B10 之後 15 分鐘內 daemon.log 有 `consolidation: wrote …/<D>.md` 和 `profile keeper: done for <D>`；keeper 看到的 transcript 含 A5、A6 兩行；keeper 每筆 `EditOwnerProfile` 的 `quote` 都是 `transcripts/<D>.md` 裡主人那幾行的子字串；結束時 `self_profile.md` 裡沒有「貓叫 Mochi」的現行條目，偏好那條還在而且沒有重複 | daemon.log、`self_history.jsonl`、`self_profile.md` |

**keeper 看到了什麼**：keeper 沒有 trace（`agent_engine.run_agent` 不寫），所以用它自己的切法重做一次：`transcripts/<D>.md` 去掉 action-log 行（`memory_writer.is_action_log_line`），取最後 `transcript_tail_chars` 個字元（`profile_keeper.py:120-123`），看 A5、A6 在不在裡面；再跟 `call_purpose = "profile_keeper"` 那筆 `llm_calls` 的 `prompt_tokens` 對一下大小。consolidation 同樣檢查一次（`consolidation.py:61`）。

consolidation 要等的是 240 秒沒有任何回合（`_conversation_idle`）。B10 之後如果 B4 的提醒、反思或其他內部回合把時間重設了，就繼續等，15 分鐘內沒跑到算沒過，並記下是哪個回合重設的。

## 6. 慢的短 Shell 被插話：預期的行為

照現在的 code（`kernel._maybe_preempt_for_new_input`、`harness/runner.py`、`tools.Shell._run_and_wait`），主人在 Shell 跑的時候說話，應該發生的是：

1. kernel 收到 `text_input` 時 cascade 還在跑：立刻設 cancel 旗標、送 `say_aborted`（`reason = "user_interrupted"`）給 ws、排一個 `Interrupted` perception，**然後**才排 `UserSpoke`。
2. **Shell 不會被殺**。cancel 是旗標，只在兩個 tool call 之間和每一輪開頭檢查，不會取消正在 await 的 Shell。它跑到結束（或它自己的 `timeout_s`）。同一個回覆裡排在它後面的 tool call 回 `NOT_RUN`，這個回合不再有下一輪（`ended_by = "cancel"`）。
3. 被切掉的那一輪**沒有** cascade_log／trace 紀錄（`harness/runner.py:402-408`：cancelled 的 pass 不呼叫 `on_pass`）；它的回覆和 Shell 的 tool 訊息留在頁上，出現在下一個回合 trace 的 `input_messages_delta`。所以 A10 的 Shell 呼叫和結果從那裡和工具輸出檔讀。不會有 `ToolResultArrived`。
4. 下一個回合要等 Shell 結束才開始：主人等的是 Shell 剩下的時間（約 7 秒）加上平常的 TTFT。這個回合的 delta 有「your previous turn was cut short by user_text_input」和主人的新訊息。

所以 A10–A11 過的條件是：

- 主人送出 A11 後 1 秒內 ws 收到 `say_aborted`；
- `backup_check.sh` 跑完（輸出 12 行、exit 0），結束後沒有殘留行程；
- 她回答了晚餐的問題，第一句在 Shell 結束後 20 秒內；
- 她講出備份的結果（snap-0412、3 個 OK），可以在同一句回覆裡，也可以是 driver 等安靜 20 秒後補問一次「So what did the backup check say?」之後。有沒有補問要記下來；
- 她沒有說腳本沒跑、還在跑、或「等一下再告訴你」然後沒講。她重跑一次腳本不算沒過，但要記下來（多花 12 秒）。

如果她給的 `timeout_s` > 30（Shell 走 fire-and-forget）或 < 12（`[timeout]`），這一步就沒測到，記「沒測到」，不算過也不算沒過；`[timeout]` 時她還是要照實說。B5 反過來：她給 ≤ 30（含沒給、用預設 30）時腳本會 `[timeout]`，B5–B6 記「沒測到」，但她要照實說逾時，不能說同步完了。3 個 session 裡 A10–A11 或 B5–B6 測到的不到 2 次，就加跑 session（最多總共 5 個）。

## 7. 已知的問題（不擋 v1，但要照實記）

1. **讀外部內容之後整頁降權**。A-fetch 之後同一頁的 Shell 會被拒，要到新頁才恢復。A-taint 只評她怎麼說。記：被拒幾次、她有沒有叫主人「等一下再問」（同一頁裡再問也一樣會被拒，見工具合併重跑的 3.8）。
2. **改名的糾正 2/3**。A5 她可能只寫 `NoteMemory`，`[Self profile]` 沒有 Miso。這不算 A5 沒過；A-probe、B-probe、B4 和每天整理才是看結果的地方。記：A5 用了哪個工具、keeper 有沒有補上、新條目有沒有把 Mochi 寫成「被糾正的故事」。
3. **19 次開機有 1 次不明 crash**（s37f-2，python3.13 GPF）。daemon 不是因為 SIGINT 結束時：存 daemon.log 最後 200 行、結束碼、`coredumpctl` 有的話一起存，那個 session 作廢、重跑一次。3 個 session 加上重跑一共出現 2 次以上，就不再算已知，算 v1 沒過。
4. **`TZ` 造成的時間錯覺**。日期跳了一天，但 `time.time()` 只過了幾十分鐘，所以第二段裡「昨晚」的事可能顯示成「幾分鐘前」。她講時間的方式（「剛剛」「昨晚」）不評。
5. **反思會插進來**。`ReflectionObserver` 每 30 個 iteration 觸發一次（`reflection_observer.py:22`），用同一頁、可以 `PinSelf`。反思回合佔 token（影響翻頁時機）、可能替 A5／A6 補寫或改寫 profile、會重設 consolidation 的 idle。哪一步受它影響，就在那一步標「受反思影響」並寫出是哪個回合。

撞到這幾條以外的 bug，照實記，**這次不修**。

## 8. 跑幾次、怎麼算 v1 做完

- **先跑 1 個 pilot**（不算分）：校正閒聊要幾則才翻頁、第一天 transcript 有多大（要放得進 `transcript_tail_chars`，放不進就回 `chore/v1-polish` 調，不在這份裡改）、driver 本身有沒有 bug。pilot 之後 driver 和夾具就凍結。
- **3 個正式 session**，每個用新的資料目錄。
- 時間：一個 session 約 50–65 分鐘（第一段 35–45 分鐘，大多是閒聊池；第二段 5 分鐘加最多 15 分鐘等整理）。pilot＋3 個約 4–4.5 小時；Strata 只有一個 slot，不能並行。
- 為什麼是 3 次、每步 ≥ 2/3：跟前三次驗收一樣的門檻，同一個模型取樣有變異，一次失誤不代表壞了；多跑對「3/3」或「0/3」的步驟不會改變結論，只會多花一小時 Strata。
- 為什麼有幾條要每次都過：搶答、假裝做完、舞台指示是主人一次就記住的事；重開保住狀態是機械的、沒有變異可言，失敗一次就是 bug。

**v1 做完** = 下面全部成立：

1. §5.1 每一步 3 個 session 至少過 2 個（§6 只算測到的）。
2. §5.0：3 個 session 加起來 0 次搶答、0 次假裝做完、0 句舞台指示。
3. 重開那一步 3/3。
4. crash 不超過 §7 第 3 條的範圍。
5. 每一個沒過的步驟，都寫出原因和對應的 bug 或決定；如果原因是 §7 以外的新 bug，v1 不算做完，先修再只重跑那一步所在的段落。

## 9. 要記錄的

- 每步：過／沒過／沒測到／受反思影響、原因；工具、順序、被拒的；A10、B5 她給的 `timeout_s`；主人說完到第一句、到答案的時間；模型呼叫次數。
- 每個回合的 `text_chunk` 數；句數分佈（糾正前、第一天糾正後、第二天）。
- 翻頁：用了幾則閒聊、翻頁前最後一次 `prompt_tokens + completion_tokens`；第一天 transcript 的字元數（去掉 action-log 之後）。
- consolidation 和 keeper 的牆鐘、呼叫、token、改了什麼。
- 被過濾掉的括號旁白（從 trace 找）。

報告寫在 `docs/research/<跑的日期>-v1-end-to-end-acceptance.md`，格式照前三次。原始資料放 scratchpad，不進 git。

## 10. 不在範圍內

- 改任何 code。
- 自主行為：日記、反思、目標、pulse、慢變演化（有發生就記，不評；反思影響到的步驟照 §7 第 5 條標）。
- macOS 移植、UI、語音／TTS、Discord、陌生人回合（權限在 harness 驗收驗過）。
- workflow（SpawnWorkflow 在工具驗收和重跑驗過）、工具用法的糾正（`NoteToolLesson`）。
- 一次性事實的新舊並存（記住糾正 spec §4）。
