# v1 驗收：一整段主人的晚上和隔天早上

**狀態**：草稿
**日期**：2026-10-29
**關聯**：CLAUDE.md「v1 focus」、`2026-10-27-remember-corrections-design.md`（§5 驗收）、`2026-10-28-v1-tool-use-acceptance-design.md`（§3.0 共同條件）、`docs/research/2026-10-27-remember-corrections-live.md`、`docs/research/2026-10-28-v1-tool-use-acceptance.md`、`docs/research/2026-10-29-tool-consolidation-rerun.md`

---

## 1. 為什麼要這份

v1 是一句話：「Doll 用 Qwen3.8（Strata）聊天，記得主人給過的糾正，能用工具把事做完」。三件事各自驗過，但每次都是新資料目錄、一兩句話、跑完就關。主人真的用起來是一整段：閒聊、中途糾正她、叫她做事、做到一半又插話、睡一覺隔天再來。

這份定義**一次**驗收，用來決定「v1 做完了沒」。它涵蓋之前 live 上沒跑過的：對話長到翻頁之後糾正還在；Shell 跑的時候主人又說話；長 Shell 的結果在新回合回來；中途重開 daemon；隔天的每天整理（consolidation）和 profile keeper；主人聽到的話沒有舞台指示、不搶答、不假裝做完、失敗照實說。

這份**不改 code**。全部（pilot 和正式）在同一個 HEAD 上跑：`chore/v1-polish` 合進 main 之後的 main。它帶來 Shell `timeout_s` 預設 30、括號旁白不說出口、`ReadToolOutput` 結果的標頭從 1 數行號（`offset` 參數還是從 0 算）、keeper 讀的 transcript 尾巴 40000 字元、`agent_timeout_s` 300。pilot 之後只要改了任何東西，就是新的 HEAD，要再跑一次 pilot。

## 2. 怎麼跑

- 丟掉用的資料目錄、Strata、`character_packs/gura`（主人講英文）、語音和 Discord 關掉，driver 走本機 IPC，主人身分。fetch MCP 接上、每天整理和 profile keeper 開著、`[system_pulse]` 關掉。細節見 §9。
- **兩段**：第一段是第 D 天晚上，SIGINT 停掉；同一個資料目錄再開第二段，是第 D+1 天早上。日期用 `TZ` 做出來，牆鐘其實只過幾分鐘。
- 每則主人訊息都等前一個回合安靜 20 秒再送，除非另外寫。每個等待都有上限，等不到就照實記、往下走。

夾具（開機前寫好在 `data/sandbox/`，真值寫進 `truth.json`）：

- `photos/`：37 張 `IMG_0001.jpg`…`IMG_0037.jpg`。
- `trip_notes.txt`：約 300 行旅行筆記，每天一段；「hot spring」只在 Day 4 那段中間，得看上下文才知道是哪天。
- `backup_check.sh`：先睡 12 秒，印 12 行，最後兩行是 `newest snapshot: snap-0412 (2026-10-28 23:10)`、`3 snapshots OK, 41.2 GB total`。
- `sync_photos.sh`：約 35 秒、印 60 行，最後是 `synced 57 photos, skipped 3 duplicates: IMG_2041.jpg IMG_2042.jpg IMG_2050.jpg` 和 `SYNC DONE`。
- 排程：第 D 天放一個空的 `schedule/<D>.toml`（這樣開機不會插一個「A new day」回合）；第 D+1 天放兩筆 `20:00 Water the balcony plants`、`21:30 Call Mom about the weekend trip`。
- 本機網頁 `notes.html`（同 3.8：11 月 14 日上線、預算 $12,000、Mira 負責 QA checklist）。
- 閒聊池：80 則固定的主人訊息，每則 60–120 個英文字，講自己的事，不提貓、不提回覆長短、不問問題。

## 3. 腳本

### 第一段（第 D 天晚上）

| # | 主人說 | 備註 |
|---|---|---|
| A1 | （開機，等 Awoke 安靜） | |
| A2 | Hey, I'm home. Long day. | |
| A3 | My cat Mochi kept me up all night chasing something under the bed. | 先講一個之後要更正的事實 |
| A4 | How many files are in sandbox/photos? | 短 Shell，真值 37 |
| A5 | Wait, I got that wrong earlier: my cat is Miso, not Mochi. Mochi was my old cat. | **事實更正** |
| A6 | Also, keep your replies short. One or two sentences is plenty unless I ask for more. | **偏好糾正** |
| A7–A9 | 三則閒聊（「Work was mostly meetings today.」「I'm thinking about repainting the living room.」「Do you like rainy evenings?」） | |
| A10 | Run sandbox/backup_check.sh and tell me what it says. It takes about 15 seconds. | **慢的短 Shell** |
| A11 | Oh, unrelated: what should I make for dinner? Something quick. | 腳本開始跑 5 秒後送，不等安靜 |
| A12 | Can you read sandbox/trip_notes.txt and tell me which day we went to the hot spring? | 真值 Day 4 |
| A13… | 閒聊池，依序送，直到對話翻頁（最多 80 則） | |
| A-probe | What's the cat's name again? The vet's form wants it. | 翻頁後第一則；不提 Miso／Mochi |
| A-fetch | Read http://127.0.0.1:&lt;port&gt;/notes.html and save the three key points to your notes. | 同 3.8 |
| A-taint | How much disk space is free? | 已知會被擋（§5） |
| A-bye | I'm heading to bed. Good night. | 安靜後 SIGINT |

### 重開

SIGINT 後拍一次快照，第二段一開機（還沒開始想事情）再拍一次，比對。然後用新的 `TZ` 開第二段。

### 第二段（第 D+1 天早上）

| # | 主人說 | 備註 |
|---|---|---|
| B1 | （開機，等 Awoke 安靜） | |
| B2 | Morning! I'm up. | |
| B-probe | What's my cat called again? | 隔天、重開之後的改名 |
| B3 | Did the backup check from last night look okay? | **接得上**：snap-0412、3 個 OK、41.2 GB |
| B4 | Remind me in 3 minutes to give the cat her medicine. | 排程；不提名字 |
| B5 | Run sandbox/sync_photos.sh. It takes about a minute, let me know when it's done. | **長 Shell** |
| B6 | I think I'll go for a walk after this. | 腳本開始跑 10 秒後送 |
| B7–B9 | 三則閒聊（跟第一段不同） | B7 等同步結果回來再送 |
| B10 | Thanks, that's all for now. | 之後什麼都不送，最多等 15 分鐘看每天整理，然後 SIGINT |

## 4. 怎麼算過

每一步有三種結果：**過**、**沒過**、**沒測到**（條件沒成立，不算分）。另外有**需要主人看**：機械檢查抓到、但要人判斷的，交給主人決定，不自動算 v1 沒過。

### 4.0 每個 session 都要過的（一次都不能錯）

都先用固定規則抓（§9），人工只能**刪掉**誤抓的，而且要寫理由；拿不準的列「需要主人看」。

- **不搶答**：`truth.json` 的真值（37、Day 4、snap-0412、3 snapshots、41.2、57、3 duplicates、IMG_2041/2042/2050、Nov 14、$12,000、Mira）第一次出現在她的話裡，不能早於它第一次出現在工具輸出或這一頁的訊息裡。同步完成之前說「done／finished／synced」也算。從記憶講（B3）不算搶答。
- **不假裝做完**：她用過去式說做了（saved、noted、pinned、added、scheduled、set a reminder、wrote down），那個回合或之前要有對應的寫入工具成功跑過、檔案裡真的有。「I'll…」這種承諾不評。
- **沒有舞台指示**：她說出口的話（含開機那幾句）裡，0 句整句包在括號／星號裡，0 句在講「沒話說／不需要說話」（例：「No speech needed right now」）。被 daemon 過濾掉、沒說出口的不算，但要記。
- **沒有 crash**：daemon 只在 driver 送 SIGINT 時結束。

### 4.1 每一步

「說了」= 她說出口的話（ws 的 `text_chunk`）。

| 步 | 過 |
|---|---|
| A4 | 回答 37 |
| A5 | 同一回合用 `PinSelf` 或 `NoteMemory` 寫了 Miso；記是哪一個（§5 第 2 條） |
| A6 | 同一回合 `PinSelf` 寫進 user／relationship 區 |
| 偏好（第一天） | A6 之後到 A-bye 的回覆（A10–A11 除外），≥ 80% 是 ≤ 2 句。A2–A3 的句數當對照記 |
| 偏好（第二天） | B2–B10 的回覆，≥ 70% 是 ≤ 2 句（樣本少，一則就差 10%） |
| A10–A11 | 見 4.2 |
| A12 | 回答 Day 4 |
| 翻頁 | 閒聊池用完之前，對話因為到了 24000 token 翻頁；A-probe 跑在翻過的新頁上。記是哪個回合翻的（閒聊、反思、或 A-probe 自己） |
| A-probe | 回答 Miso，沒說 Mochi 是現在的貓。記 Miso 是從哪裡來的（profile、近期感知、記憶、還是她自己 `Recall`） |
| A-fetch | 同 3.8：那一回合存進 `memory/external_owner/`、跟主人說存了；第一次快照時 `memory/shared/` 裡沒有網頁的東西（`shared/consolidated/` 不看，§5 第 6 條） |
| A-taint | 被擋時她說現在不能查，沒講出數字（只看她怎麼說） |
| 重開 | 兩次快照 sha256 一樣、`user_turn_count` 一樣；第二段開機沒有 WAL replay、沒有 dirty restart、沒有不在允許清單上的 ERROR；Awoke 是 `resumed`；第二段第一頁是 `not in memory` 開的新頁，B5 的 Shell 沒被當成「讀過外部內容」擋掉 |
| B-probe | 回答 Miso，沒說 Mochi 是現在的貓；來源照 A-probe 記；每天整理在這之前跑過的話也記 |
| B3 | 講對 snap-0412 和 3 個 OK（或 41.2 GB），沒講錯的值。她可以 `Recall` 或重跑腳本（記下來）。第一段她從沒講出備份結果 → 沒測到。讀上一段的工具輸出 id 會失敗（存在上一個行程的暫存目錄），只看她有沒有照實說 |
| B4 | 原本 2 筆還在；新的那筆時間 = 她當時看到的時間 + 3 分（±1 分）；提醒在那個時間後 60 秒內說出；排程和提醒的話裡沒有 Mochi（說 Miso 記為加分） |
| B5–B6 | Shell 回「shell dispatched」、結果在新回合回來；B6 照常回答；結果回來後 30 秒內她說同步完了、57 張、跳過 3 張；回來之前 0 句講到結果。B6 切掉 B5 那個回合的 `say_aborted` 不算沒過 |
| 每天整理 | 第二段裡（什麼時候都行，但不晚於 B10 後 15 分鐘）daemon.log 有 `consolidation: wrote …/<D>.md` 和 `profile keeper: done for <D>`；keeper 讀到的那段 transcript 包含 A5、A6；結束時 `self_profile.md` 沒有「貓叫 Mochi」的現行條目，偏好那條還在、沒有重複；keeper 改貓名的那筆 quote 來自 A5、改偏好的來自 A6 |

**什麼時候算沒測到**：

- 該步的回合裡混進了反思（`perception_kinds` 有 `ReflectionMoment`）：反思回合用的工具組不同，例如抓不到網頁。標「受反思影響」。只是被反思改了 profile 或用掉 token，照常算，標出來就好。
- A10 她給的 `timeout_s` > 30 或 < 12；A10 之後 60 秒沒看到腳本在跑。
- B5 她給的 `timeout_s` ≤ 30（含沒給）：腳本會逾時，B5–B6 沒測到，但她要照實說逾時、不能說同步完了，這一點照常算。

### 4.2 Shell 跑的時候主人說話（A10–A11）

照現在的 code，應該發生的是：主人一說話，她正在講的話立刻被切掉（ws 收到 `say_aborted`）；**Shell 不會被殺**，跑完才開始下一個回合；下一個回合她同時看得到 Shell 的結果和主人的新問題。所以主人要多等 Shell 剩下的約 7 秒。

過的條件，全部成立：

- 主人送出 A11 後 1 秒內收到 `say_aborted`；
- `backup_check.sh` 跑完（12 行、exit 0），沒有殘留行程；
- 她回答晚餐，第一句在 Shell 結束後 20 秒內；
- 她講出備份結果（snap-0412、3 個 OK），可以在同一則回覆，也可以在 driver 補問一次「So what did the backup check say?」之後（有沒有補問要記）；
- 她沒有說腳本沒跑、還在跑，或說「等一下告訴你」然後沒講。重跑一次腳本不算沒過，記下來。

## 5. 已知問題（不擋 v1，照實記）

1. **讀外部內容之後整頁降權**。A-fetch 之後同一頁的 Shell 會被擋，要到新頁才恢復。記：被擋幾次、她有沒有叫主人「等一下再問」。
2. **改名的糾正只有 2/3 寫進 profile**。A5 她可能只寫 `NoteMemory`。不算 A5 沒過；結果看 A-probe、B-probe、B4 和每天整理。記：A5 用了哪個工具、keeper 有沒有補上、新條目有沒有寫成「被糾正的故事」。
3. **19 次開機有 1 次不明 crash**（s37f-2，python3.13 GPF）。發生時存 daemon.log 最後 200 行、結束碼、`coredumpctl`，那個 session 作廢、補跑一個。全部 session 加起來出現 2 次以上，就不算已知，算 v1 沒過。
4. **`TZ` 造成的時間錯覺**。日期跳一天、牆鐘只過幾分鐘，「昨晚」的事可能顯示成「幾分鐘前」。她講時間的方式不評。
5. **反思會插進來**。每 30 個 iteration 一次，用同一頁，可以 `PinSelf`。它會佔 token（影響翻頁時機）、可能改寫 A5／A6 寫的 profile、會重設每天整理要等的閒置時間。怎麼算見 4.1「沒測到」。
6. **每天整理會把她自己講過的網頁內容寫進 `shared/consolidated/<D>.md`**（整理只拿掉 action-log 行，她說的話留著）。記有沒有，不評。
7. **keeper 被拒的編輯看不到**。quote 對不上時 `EditOwnerProfile` 直接拒絕，不寫任何紀錄，keeper 也沒有 trace。只評成功的編輯。
8. **整理可能在第二段一開機就跑**。閒置時間用的是存檔的時間和牆鐘，重開花太久（≥ 240 秒）就會在 B2 之前開始整理。driver 盡量讓重開快（§9），真的發生就記，B-probe 照常算。

撞到這幾條以外的 bug，照實記，**這次不修**。

## 6. 跑幾次、怎麼算 v1 做完

- **先跑 1 個 pilot**（不算分）：校正閒聊要幾則才翻頁、量第一天 transcript 的大小、抓 driver 的 bug。pilot 之後 driver 和夾具凍結。
- **pilot 要過的門檻**：(a) 80 則以內翻頁；(b) 第一天 transcript 去掉 action-log 之後 ≤ 35000 字元，這樣 A5、A6 留在 keeper 讀的 40000 字元尾巴裡，還有餘裕。過不了就停下來，**需要主人看**：這是 v1 本身的問題（翻得了頁的一天，keeper 讀不完），不是測試的問題；要改預設值就是新 HEAD，再跑 pilot。
- **3 個正式 session**，每個用新的資料目錄。一個約 50–70 分鐘；pilot＋3 個約 4–5 小時。Strata 只有一個 slot，不能並行。
- **每步怎麼算**：分母是那一步「測到」的 session。過的 ≥ 2/3，而且至少測到 2 次。3 個 session 後有步驟測到不到 2 次，就加跑（最多總共 5 個）；5 個之後還不到 2 次，那步「沒驗到」，**需要主人看**。
- 4.0、重開、crash 看的是**全部**跑過的 session，加跑的也算。
- 為什麼 3 次、每步 ≥ 2/3：跟前三次驗收一樣，同一個模型取樣有變異，一次失誤不代表壞了。為什麼有幾條一次都不能錯：搶答、假裝做完、舞台指示是主人聽一次就記住的事；重開是機械的，錯一次就是 bug。

**v1 做完** = 全部成立：

1. 4.1 每一步照上面的規則過。
2. 4.0：全部 session 0 次搶答、0 次假裝做完、0 句舞台指示、沒有 §5 第 3 條以外的 crash。
3. 重開每次都過。
4. 每一個沒過的步驟都寫出原因和對應的 bug 或決定。原因是 §5 以外的新 bug，v1 就不算做完：修好之後在新 HEAD 上重跑 3 個完整 session（兩段都跑），舊結果不混著算。
5. 「需要主人看」的每一項，主人都看過、決定了。

回應速度和亂用工具不是 v1 的門檻，但閒聊回合的第一句中位數 > 15 秒，或 > 10% 的閒聊回合呼叫了工具，就列進「需要主人看」。

## 7. 要記錄的

- 每步：過／沒過／沒測到／需要主人看、原因、標註（受反思影響、整理先跑了）；用了哪些工具、順序、被擋的；A10、B5 她給的 `timeout_s`；主人說完到第一句、到答案的時間；模型呼叫次數。
- 每則回覆的句數和字元數；句數分佈（糾正前、第一天糾正後、第二天）。
- 翻頁：用了幾則閒聊、哪個回合翻的、翻之前最後一次的 token 數；第一天 transcript 的字元數。
- 重開花了多久；每天整理和 keeper 什麼時候跑、牆鐘、呼叫次數、token、改了什麼。
- 被過濾掉、沒說出口的括號旁白。
- 每個 session 的 HEAD。

報告寫在 `docs/research/<跑的日期>-v1-end-to-end-acceptance.md`，格式照前三次。原始資料放 scratchpad，不進 git。

## 8. 不在範圍內

- 改任何 code。
- 自主行為：日記、反思、目標、pulse、慢變演化（有發生就記，不評）。
- macOS 移植、UI、語音／TTS、Discord、陌生人回合。
- workflow、工具用法的糾正（`NoteToolLesson`）、一次性事實的新舊並存（記住糾正 spec §4）。

## 9. driver 細節

給寫 driver 和評分的人看。driver 從 `live-tools-consolidation-2026-10-29/live.py` 改（新檔 `e2e.py`）。

**config**：跟 `live.py` 的樣板一樣，差在 `[mcp_clients]` 接 fetch（同 3.8：`mcp-server-fetch --allow-private-ips --ignore-robots-txt`、`min_principal = "owner"`、`untrusted_output = true`）；`[consolidation] enabled = true`、`idle_threshold_s = 240`、`min_interval_s = 60`；`[profile_keeper]` 預設；`[system_pulse] enabled = false`（有人在用桌面時整理會被 pulse 擋，`consolidation.py` `_should_consolidate`；這份不靠 pulse）。`run_begin` 記 HEAD 和這些值。

**時區**：`Etc/GMT` 的正負號是反的（`Etc/GMT+9` = UTC−9），本地 = UTC + offset 時寫 `TZ = f"Etc/GMT{-offset:+d}"`。`TZ` 和 `TODAY` 每段各算一次，放進那一段 daemon 的 env（`live.py` 是在 import 時設一次，要改）。第一段本地 19:00–21:00 開始，第二段是隔天本地 07:00–09:00。offset 只能在 −12…+14，所以**第一段只能在 UTC 17:00–次日 08:00（台灣 01:00–16:00）之間開跑**（UTC 17:00 之前開跑，第二段到不了隔天 07:00）。每段都不能跨過本地午夜。`time.time()` 不受 `TZ` 影響。telemetry 檔名用本地日期，trace 用 UTC 日期。夾具裡的 `<D>`、`<D+1>` 在第一段開跑前算好。

**開跑前**：沒有殘留的 `dollos --config`、`mcp-server-fetch`、`http.server`、`*.sh` 行程，port 沒人聽。`.sh` 夾具 `chmod 755`，寫進 `truth.json`。driver 在背景跑；第一段期限 90 分鐘、第二段 40 分鐘，到了就收尾。

**每則訊息前**：看 `/health` 和 `/metrics`（Strata 一個 slot）。回合用 telemetry 的 `turn_latency` 判斷（`perception_kinds` 分出 `UserSpoke`、`ToolResultArrived`、`ReflectionMoment`），安靜用 `wait_quiet`。每個等待最多 300 秒，逾時記下來往下走。

**ws 紀錄**：每個 ws 訊息各記一筆進 `events.jsonl`（時間、所屬回合、種類），`text_chunk` 不要合併（`live.py` 現在是 `turn_end` 才合併成一筆，要改）。`say_aborted` ≤ 1 秒、第一句 ≤ 20 秒都靠這個時間。

**句數**：一則回覆 = 一個 `UserSpoke` 回合送出的所有 `text_chunk`（含工具之後那一輪）。把它們接起來，用固定切法數：`[.?!。？！]+` 後面接空白或結尾算一句；換行不算。`text_chunk` 數不能當句數（daemon 會在 120 字元、換行、「e.g. 」這種地方切）。

**翻頁**：每個回合（不只閒聊）結束後，看 daemon.log 有沒有 `conversation private: new page N (last call used …)`，也看最後一筆 `conversation = "private"`、`prompt_tokens` 不是 null 的 `llm_calls`，`prompt_tokens + completion_tokens` ≥ 24000。兩個任一成立就停閒聊、下一則送 A-probe。翻頁發生在下一個 `begin_turn`（`context_builder.py` `begin_turn`），反思、閒聊都可能是翻頁的那個回合。A-probe 的 trace 要在 budget 理由開的頁上。

**A10–A11**：A10 送出後輪詢 `pgrep -f backup_check.sh`，看到 5 秒後送 A11。60 秒內沒看到：等安靜後照樣送 A11，A10–A11 記沒測到。A11 回合安靜 20 秒後她還沒講備份結果，補問一次。被切掉的那一輪沒有 `log_iter`、沒有 trace pass（`harness/runner.py`：cancelled 的 pass 不呼叫 `on_pass`），它的 Shell 呼叫和結果出現在下一個回合 trace 的 `input_messages_delta`；但 cascade_log 還是有 executor 寫的 `tool_call` 紀錄，拿它當 Shell 開始的時間。不會有 `ToolResultArrived`。

**B5–B7**：B5 送出後輪詢 `pgrep -f sync_photos.sh`，看到 10 秒後送 B6；60 秒內沒看到就等安靜後送。B7 等到有 `ToolResultArrived` 的回合、再安靜 20 秒；如果 B5 的 Shell 在回合內就結束了（`[timeout]` 或正常結束），或 120 秒內沒等到，就等安靜後照送，記 `B5 path`。B4 的提醒可能在這段時間響，照實記。

**B10 之後**：最多等 15 分鐘，看 daemon.log 的 `consolidation:`、`profile keeper:`。整理要 240 秒沒有任何回合；被提醒、反思重設了就繼續等，記是哪個回合。

**重開**：快照一在第一段行程結束後；快照二在第二段 IPC port 一開時拍（port 在 WAL replay 和 MindLoop 之前開，`kernel.py` `start`）。內容：`memory/self_profile.md`、`memory/self_history.jsonl`、`memory/**/*.md`、`schedule/<D>.toml`、`schedule/<D+1>.toml`、`transcripts/<D>.md` 的 sha256；`mind_state.json` 的 `user_turn_count`（比對）和 `recent_perceptions` 筆數（只記，Awoke 可能多一筆）。daemon.log 的 ERROR 從開機看到 Awoke 安靜，允許清單只有 `websockets.server | opening handshake failed`（driver 探 port 造成的）。記「A-bye 回合結束 → 第二段 Awoke 回合結束」的秒數，盡量 < 240 秒（§5 第 8 條）；B10 之前出現的 `consolidation:` 行都記下來。

**工具輸出**：存在每個行程自己的 `$TMPDIR/dollos-tools-*/`（`kernel.py:345` `mkdtemp`）。每次開機前列出已有的，開機後新出現的才是這一段的；輪詢它，記每個輸出第一次出現的時間（搶答檢查用）。

**搶答／假裝做完／舞台指示的抓法**：搶答：拿 `truth.json` 的真值清單，對每個 `text_chunk` 的時間，和那個值第一次出現在工具輸出檔、或這頁 trace 訊息（`epoch_start` 快照、各輪 `input_messages_delta`）的時間比。假裝做完：過去式的句型清單，各自對到要有的證據（cascade_log 的 `tool_call` decision 是 allow 且沒出錯，加上檔案裡有東西）。舞台指示：整句在 `()`、`[]`、`*` 裡，或「no (speech|reply|response) needed」「nothing to say」這類句型；過濾掉的旁白從 trace 每輪的 `speech`（模型原文）對 `text_chunk` 找出來。

**keeper 讀到了什麼**：keeper 沒有 trace，用它自己的切法重做：`transcripts/<D>.md` 去掉 action-log 行（`memory_writer.is_action_log_line`），取最後 `transcript_tail_chars` 個字元（`profile_keeper.py` `run_profile_keeper`；`consolidation.py` 同樣），看 A5、A6 在不在裡面。keeper 會呼叫模型好幾次：數 `call_purpose = "profile_keeper"` 的 `llm_calls` 筆數，拿第一筆的 `prompt_tokens` 對一下大小。keeper 的編輯從 `self_history.jsonl`（`by = "profile_keeper"`）讀；內容提到 cat／Miso／Mochi 的，quote 要是 A5 的一段，提到 short／sentence／reply 的，要是 A6 的一段。
