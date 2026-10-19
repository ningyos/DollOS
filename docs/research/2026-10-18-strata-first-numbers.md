# Strata 第一批數字（對真的 server 跑一次）

**Plan**：`docs/superpowers/plans/2026-10-18-model-adapter-chat-api.md` Task 11
**日期**：2026-10-19，07:53–08:08 UTC
**Code**：`feat/model-adapter-chat` @ `b1e589d`（Task 0–10 做完的狀態），沒有為這次改任何 code
**Server**：`http://100.110.158.36:8080`，Strata 0.1.40，`qwen3.8-flash-next-iq3_s`

---

## 一句話

對話只往後加是有效的：46 次呼叫裡，接續的 41 次有 35 次命中、5 次退回上一個 prompt 結尾、1 次沒命中；主人回合命中時，開始思考前中位數 2.9 秒，第一句中位數 5.6 秒（思考很長的一次是 33.8 秒）。沒過的有三件：**私人對話她全講中文**；第 3 回合在一個別的 client 的小 request 之後**整段重算**（34.6 秒）；第 4、5 回合沒有照 plan 呼叫 `Recall`、`WriteSchedule`（理由見下）。

---

## 怎麼跑的

- 一份丟掉用的 config：Strata、`[data] root` 和 `[trace] root` 指到 scratchpad 裡的新目錄、語音和 Discord 沒開、IPC port 19876、`think_budget_tokens = 1024`、`conversation_budget_tokens = 24000`。`[consolidation] enabled = false`（plan 說 Task 11 不跑背景 agent）；evolution 照預設開著，新的資料目錄不會觸發它，開著是為了讓 internal 的工具清單跟平常一樣（有 `SelfRevision`）。
- 一支 scratchpad 裡的 driver，講 `experiments/ws_client.py` 的協定（`text_input` → `text_chunk`/`turn_end`），但只開一條連線，從 `turn_latency` 的 JSONL 判斷回合結束；每個 request 前看 `/health` 和 `/metrics`，有人在用就等。不直接用 `ws_client.py` 的原因：連上的那一刻 daemon 會推一個「今天還沒有計劃」的 ScheduledMoment，那個回合的 `turn_end` 會讓一次性的 client 提早結束。外部那段用同一條連線送 `channel_register`/`channel_event`（payload 跟 MCP server 送的一樣：`is_dm`、非主人）。
- 三次執行，同一個資料目錄：
  - **A**：開機 → 第 1、2 回合 → driver 直接對 server 送一個不相干的小 request（別的 system，37 token）→ 第 3 回合 → 外部訊息（`ch:` 對話）→ 第 4、5 回合。
  - **B（換頁）**：重開，`conversation_budget_tokens = 10278`（第 1 回合的 `prompt_tokens` 8278 + 2000），一直送主人回合到出現新的一頁，再多一回合。
  - **C（重開）**：重開，等她安靜下來，送一個回合。
- 第 1 回合不是新的一頁：daemon 開機自己會跑一個 Awoke 回合，連上時又跑一個排程回合，新的一頁由 Awoke 開。所以重開的代價量了兩次（B、C 的 Awoke）。
- 期間沒有別的 client：server 的 `requests` 從 226 到 273，剛好是 46 次 DollOS 呼叫加 1 個 driver 的小 request。開始前停著 4 段別人的對話（其中一段約 106k token），這次跑完被擠掉 7 段（`evictions` 74 → 81），別人下一次會整個重算。

判斷命中（plan 的規則，`think_capped` 是 False 的才算；這次 46 次都沒觸發）：`prompt_n ≤ new + 64` 是命中；`≤ new + 上一次的 completion + 64` 是退回上一個 prompt 結尾；其他是沒命中。`new` 是 `new_prompt_tokens`。

---

## 過關檢查

| 項目 | 結果 |
|---|---|
| 開機檢查 | 過。model 設成 `qwen3.8-not-a-model`：拒絕開機，IPC 沒起來，訊息是 `model server at http://100.110.158.36:8080 (configured model 'qwen3.8-not-a-model') does not serve that model: /v1/models returned HTTP 200, ids ['qwen3.8-flash-next-iq3_s']` |
| 5 個回合沒有錯誤 | 過。46 次呼叫 0 個錯誤，0 次 `length` |
| 回覆是英文 | **沒過**。私人對話每一句都是中文（A、B 前半是繁體，B 換頁之後和 C 混進簡體）；外部 `ch:` 那段是英文 |
| 唸出來的文字沒有格式殘渣 | `<tool_call>`、`<think>`、`SEEN:` 都沒有。但 C 有 3 個回合唸出單獨一個「.」（見發現 5） |
| 第 3 回合 `NoteMemory` | 過。先呼叫 `NoteMemory`，第二輪說「記下了,週五(10/24)下午三點看牙醫。」，`memory/shared/2026-10-19.md` 有那則筆記 |
| 第 4 回合 `Recall` | **沒照 plan**。她沒呼叫 `Recall`，直接答對了：筆記就在同一頁的對話裡（對話只往後加，她記得幾分鐘前的事）。`Recall` 在 B 的 Awoke 回合有用到：第二輪接回 557 token 的結果，5.2 秒 |
| 第 5 回合 `WriteSchedule` | **沒照 plan**。`WriteSchedule` 只能寫今天的排程，週四排不進去；她改用 `SpawnMonitor`（`sleep` 3 天 11 小時再 echo）加 `OpenLoop`。巢狀的 `entries` 在開機的排程回合有測到：4 筆，正確存進 `schedule/2026-10-19.toml` |
| 命中 | **沒過一次**。第 3 回合的第一輪沒命中（`cache_n` 0，8585 token 整個重算，34.6 秒）。其他主人回合和所有第二輪都是命中或退回 |
| 新訊息 ≤ 50 token 的命中 < 1 秒 | 過。22 → 0.48 秒、39 → 0.77 秒、45 → 0.99 秒、19 → 0.40 秒 |
| 跨對話接回 | 一半。第 4 回合排在 `ch:` 對話後面：接回（`restores` +1），117 token、2.7 秒。第 3 回合排在小 request 後面：沒接回（見發現 2） |
| 換頁 | 過。B 的 R6 開新的一頁（`last call used 10390 >= 10278`）：`cache_n` 7394（就是工具 + system），新算 1017，7.1 秒。下一回合 R7 是退回，不是命中（4.3 秒） |
| 重開 | 過。重開後第一個回合開新的一頁，`cache_n` 7394，6.4 秒（B）、7.2 秒（C） |

---

## 每個回合

TTFT 是第一輪從送出到第一個片段（含排隊和 prefill）；「第一句」是 `first_speak_ms`（第一句交給 sink）；energy 是 vitals 記的這回合扣的量。

| 回合 | 對話/頁 | 輪數 | 工具 | TTFT | 第一句 | 思考字數 | 整回合 | energy | 第一輪 |
|---|---|---|---|---|---|---|---|---|---|
| A Awoke | private/1 | 1 | – | 31.0 s | 34.0 s | 327 | 34.7 s | 0.0828 | 新的一頁 |
| A 排程開機 | private/1 | 2 | WriteSchedule | 3.3 s | 12.3 s | 436 | 12.3 s | 0.0134 | 命中 |
| A T1 | private/1 | 1 | – | 2.8 s | 4.1 s | 145 | 5.5 s | 0.0049 | 命中 |
| A T2 | private/1 | 1 | – | 1.1 s | 3.7 s | 298 | 5.5 s | 0.0057 | 命中 |
| A T3 | private/1 | 2 | NoteMemory | 34.6 s | 39.7 s | 241 | 40.1 s | 0.0069 | **沒命中** |
| A CH | ch:mcp/1 | 1 | – | 16.6 s | 18.6 s | 219 | 18.8 s | 不扣（外部） | 新的一頁 |
| A T4 | private/1 | 1 | – | 2.8 s | 5.0 s | 192 | 5.3 s | 0.0044 | 命中 |
| A T5 | private/1 | 2 | SpawnMonitor, OpenLoop | 2.8 s | 33.8 s | 2663 | 34.5 s | 0.0413 | 命中 |
| B Awoke | private/1 | 2 | Recall | 6.4 s | 16.0 s | 439 | 17.8 s | 0.0989 | 新的一頁 |
| B R1 | private/1 | 1 | – | 3.0 s | 9.8 s | 779 | 13.3 s | 0.0118 | 命中 |
| B R2 | private/1 | 1 | – | 2.9 s | 5.6 s | 286 | 8.3 s | 0.0069 | 命中 |
| B R3 | private/1 | 1 | – | 2.9 s | 8.4 s | 667 | 12.1 s | 0.0121 | 命中 |
| B R4 | private/1 | 1 | – | 4.6 s | 7.6 s | 284 | 9.7 s | 0.0068 | 退回 |
| B R5 | private/1 | 1 | – | 3.3 s | 6.0 s | 212 | 8.5 s | 0.0058 | 退回 |
| B R6 | private/2 | 1 | – | 7.1 s | 10.2 s | 256 | 13.7 s | 0.0942 | 新的一頁 |
| B R7 | private/2 | 2 | PursueGoal, PinSelf | 4.3 s | 7.5 s | 576 | 26.4 s | 0.0234 | 退回 |
| C Awoke | private/1 | 4 | Scratchpad, Shell, Shell | 7.2 s | 12.0 s | 844 | 33.2 s | 0.1119 | 新的一頁 |
| C 工具結果 1 | private/1 | 5 | ReadToolOutput, GrepToolOutput, Shell, Shell | 4.4 s | 45.8 s | 847 | 45.8 s | 0.0613 | 命中 |
| C 工具結果 2 | private/1 | 3 | ReadToolOutput, Shell | 3.7 s | 35.4 s | 1604 | 35.4 s | 0.0396 | 命中 |
| C 工具結果 3 | private/1 | 3 | ReadToolOutput, Shell | 3.0 s | 17.7 s | 166 | 17.7 s | 0.0163 | 命中 |
| C 工具結果 4 | private/1 | 2 | Shell | 3.9 s | 14.8 s | 291 | 14.8 s | 0.0150 | 命中 |
| C 工具結果 5 | private/1 | 2 | Shell | 3.2 s | 10.0 s | 125 | 10.0 s | 0.0087 | 命中 |
| C 工具結果 6 | private/1 | 5 | ReadToolOutput, NoteMemory, NoteMemory, AdvanceGoal, NoteToolLesson, Scratchpad | 3.5 s | 30.5 s | 3485 | 79.3 s | 0.0928 | 命中 |
| C RS1 | private/1 | 1 | – | 4.5 s | 7.5 s | 229 | 9.4 s | 0.0085 | 命中 |


---

## 彙總

| 情況 | 次數 | 開始思考前（TTFT） | 新算的 token（`prompt_n`） |
|---|---|---|---|
| 命中，主人回合的第一輪 | 8 | 1.1–4.5 秒，中位數 2.9 秒 | 45–333 |
| 命中，其他回合的第一輪（排程、工具結果） | 7 | 3.0–4.4 秒 | 121–279 |
| 命中，工具之後的下一輪，結果短 | 15 | 0.5–3.7 秒 | 19–199 |
| 命中，工具之後的下一輪，`Recall` 或 Shell 的大輸出 | 5 | 5.1–7.8 秒 | 558–1385 |
| （上面命中裡，新算 ≤ 50 token 的） | 4 | 0.4–1.0 秒 | 19–45 |
| 退回上一個 prompt 結尾 | 5 | 3.3–5.2 秒，中位數 4.3 秒 | 185–526 |
| 新的一頁，前綴還停在 server 上 | 3 | 6.4 / 7.2 / 7.2 秒 | 804 / 1017 / 1152 |
| 新的一頁，什麼都沒停（第一次開機） | 1 | 31.0 秒 | 7571 |
| 外部新 channel 的第一頁 | 1 | 16.6 秒 | 3792 |
| 沒命中（第 3 回合） | 1 | 34.6 秒 | 8585 |

- **固定前綴**（internal 的工具 JSON + system）是 7394 token。三次新的一頁 `cache_n` 都剛好是 7394，所以 system 的結尾確實是存檔點，換頁和重開都接得回。
- **開口**：命中的主人回合，7 個的第一句在 3.7–9.8 秒（中位數 5.6 秒）。TTFT 之後的部分幾乎都是思考：T1 思考 145 字，第一句比 TTFT 晚 1.3 秒；R1 思考 779 字，晚 6.8 秒。T5 思考 2663 字（872 completion token），第一句 33.8 秒。
- **退回**：5/41（12%）。A 沒有、B 有 4 次（R4、R5、R7 兩輪）、C 有 1 次。B 的 4 次每次都伴隨 server 的 `parks` +1；每次多 1.5–2 秒。
- **思考上限**：46 次都沒觸發（1024 token 的上限）。
- **解碼**：中位數 29.6 tok/s（20.1–35.8；很短的回覆偏低）。
- **對話長度**：C 裡她自己跑研究，private 從 8.5k 長到 20.4k token，除了一次退回都命中；長度本身不影響命中時的速度（20k 時新算 19 token 仍是 0.4 秒）。
- **Energy**：主人回合（T5 以外）每回合扣 0.0044–0.0234（中位數 0.0069），T5 0.041；工具結果回合 0.009–0.093；新的一頁 0.083–0.112，因為新的一頁的 `prior_tokens` 從 0 算，整頁 8k 都算新的（server 其實重用了 7.4k）。spec 原本把典型回合校準在 0.05，現在聊天回合約小 7 倍。`token_per_energy_unit` 要不要調，留給你決定。

---

## 失敗和發現

1. **私人對話全講中文**（沒過）。外部那段講英文，所以不是模型不會。她看到的中文：`[Mind state]` 的標籤（`mood: 平靜`、`精力: 飽滿`）、連上時推的 ScheduledMoment（`kernel.py` 的 `_maybe_bootstrap_plan`：「今天還沒有計劃，請呼叫 WriteSchedule 安排今天的行程。」）、外部訊息的框（`[AI peer 私訊]…`）、`[Memory guideline]`（記憶用繁體中文寫）、部分工具說明。第一個回覆（Awoke，那時還沒有 ScheduledMoment）就已經是中文，之後整頁都跟著中文。這次沒有再拆是哪一個造成的。**後續（2026-10-19）**：主因是工具說明的中文；daemon 自己的字全換成英文後（`feat/gura-english-speech`），離線 28 個 request 加 live 9 次呼叫，有講話的回覆 18/18 英文、0 句中文。見 `2026-10-19-gura-english-speech-results.md`。
2. **第 3 回合整段重算**。driver 的小 request（37 token、2.6 秒）跑完之後，server 的 `parks` 沒有增加，也就是 private 那段沒被停放，直接被蓋掉；下一個 private 呼叫 `cache_n` 0，8585 token 重算 34.5 秒，連 system 結尾的存檔點都沒有。對照：外部 `ch:` 那段（3792 token）進來時 `parks` +1，第 4 回合就接回來了。只看到一次，要在 server 那邊重現才能確定是不是「短的 request 不觸發停放」。這跟 plan「就算中間跑過別的對話，也接得回來」不一致。
3. **`[Cognition]` 讓主人回合多 1.7 秒**。13 個主人回合有 7 個的 delta 帶 `[Cognition]`（約 70 token），因為「thought weight: light/steady」照上一次呼叫的延遲在跳。帶的時候新訊息約 120 token、2.8 秒；什麼區塊都不帶時約 45 token、1.0 秒。另外「mind room」讀的是任何對話的最後一次呼叫：第 4 回合顯示「4k of 131k」，那是 `ch:` 那段的長度。
4. **第 4、5 回合沒照 plan**。第 4 回合用不到 `Recall`（答案在同一頁裡）；第 5 回合 `WriteSchedule` 只能寫今天。她改用的 `SpawnMonitor` 是 daemon 的子行程，daemon 一重開就沒了（這次就重開了），只剩 `OpenLoop` 記著。`PinSelf` 在主人回合被呼叫一次（R7），照設計被擋下（`rejected_unbound`，不算失敗）。
5. **唸出單獨的「.」**。C 的工具結果 3、4、5 三個回合，她的話只有一個「.」，照樣送進 sink（會被 TTS 唸）。`_emit_sentence` 擋空白句，沒擋只有標點的句子。
6. **外部每個新 channel 都是冷的**。`ch:` 的工具清單不同，第一頁 3792 token 要 16.6 秒。MCP 每次 `talk()` 開一個新 channel，所以每則 MCP 訊息都會付這個代價，除非那個前綴還停在 server 上。
7. **重開之後她自己動起來**。C 開機後，她接著 B 裡用 `PursueGoal` 立的目標，在主人說話前自己跑了 Awoke 加 6 個工具結果回合、24 次呼叫、約 4 分鐘：`Shell` 跑 `grep -ril … ~/` 和 `curl` 維基百科。功能上是對的（fire-and-forget 的結果以 `ToolResultArrived` 回來、每輪命中），但這段時間 slot 被她佔著。

---

## 附錄：每次呼叫

`new` 是 `new_prompt_tokens`；`cache_n`、`prompt_n`、`prompt_ms` 是 server 的 `timings`，跟 `/metrics` 的 `requests` 逐筆對過（`reused`、`prompt_read`、`prompt_ms` 都一致）。

| # | 回合 | 對話/頁 | prompt | new | compl | cache_n | prompt_n | prompt_ms | TTFT | tok/s | finish | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A Awoke | private/1 | 7571 | 7571 | 95 | 0 | 7571 | 30.90 s | 31.00 s | 25.5 | stop | 新的一頁 |
| 2 | A 排程開機 | private/1 | 7873 | 207 | 173 | 7667 | 206 | 3.26 s | 3.35 s | 32.2 | tool_calls | 命中 |
| 3 | A 排程開機 | private/1 | 8067 | 21 | 92 | 8045 | 22 | 0.48 s | 0.57 s | 29.7 | stop | 命中 |
| 4 | A T1 | private/1 | 8278 | 119 | 89 | 8159 | 119 | 2.71 s | 2.80 s | 32.2 | stop | 命中 |
| 5 | A T2 | private/1 | 8413 | 46 | 125 | 8368 | 45 | 0.99 s | 1.08 s | 28.2 | stop | 命中 |
| 6 | A T3 | private/1 | 8585 | 47 | 104 | 0 | 8585 | 34.48 s | 34.58 s | 31.5 | tool_calls | **沒命中** |
| 7 | A T3 | private/1 | 8730 | 41 | 40 | 8691 | 39 | 0.77 s | 0.86 s | 29.2 | stop | 命中 |
| 8 | A CH | ch:mcp/1 | 3792 | 3792 | 68 | 0 | 3792 | 16.53 s | 16.60 s | 30.0 | stop | 新的一頁 |
| 9 | A T4 | private/1 | 8888 | 118 | 75 | 8771 | 117 | 2.71 s | 2.80 s | 29.7 | stop | 命中 |
| 10 | A T5 | private/1 | 9082 | 119 | 872 | 8963 | 119 | 2.73 s | 2.82 s | 32.6 | tool_calls | 命中 |
| 11 | A T5 | private/1 | 10060 | 106 | 62 | 9956 | 104 | 2.76 s | 2.87 s | 29.6 | stop | 命中 |
| 12 | B Awoke | private/1 | 8198 | 8198 | 101 | 7394 | 804 | 6.36 s | 6.44 s | 27.6 | tool_calls | 新的一頁 |
| 13 | B Awoke | private/1 | 8856 | 557 | 83 | 8298 | 558 | 5.08 s | 5.17 s | 32.2 | stop | 命中 |
| 14 | B R1 | private/1 | 9080 | 141 | 249 | 8938 | 142 | 2.89 s | 2.99 s | 24.1 | stop | 命中 |
| 15 | B R2 | private/1 | 9447 | 118 | 136 | 9330 | 117 | 2.79 s | 2.90 s | 24.8 | stop | 命中 |
| 16 | B R3 | private/1 | 9704 | 121 | 259 | 9583 | 121 | 2.79 s | 2.89 s | 28.0 | stop | 命中 |
| 17 | B R4 | private/1 | 10083 | 120 | 134 | 9699 | 384 | 4.54 s | 4.65 s | 26.2 | stop | 退回 |
| 18 | B R5 | private/1 | 10263 | 46 | 127 | 10078 | 185 | 3.21 s | 3.32 s | 24.2 | stop | 退回 |
| 19 | B R6 | private/2 | 8411 | 8411 | 158 | 7394 | 1017 | 7.05 s | 7.15 s | 24.0 | stop | 新的一頁 |
| 20 | B R7 | private/2 | 8716 | 147 | 421 | 8406 | 310 | 4.18 s | 4.28 s | 29.6 | tool_calls | 退回 |
| 21 | B R7 | private/2 | 9237 | 100 | 79 | 8711 | 526 | 5.08 s | 5.18 s | 28.3 | stop | 退回 |
| 22 | C Awoke | private/1 | 8546 | 8546 | 261 | 7394 | 1152 | 7.13 s | 7.22 s | 27.2 | tool_calls | 新的一頁 |
| 23 | C Awoke | private/1 | 8830 | 23 | 95 | 8541 | 289 | 4.06 s | 4.16 s | 35.2 | tool_calls | 退回 |
| 24 | C Awoke | private/1 | 8994 | 69 | 119 | 8924 | 70 | 2.45 s | 2.55 s | 35.0 | tool_calls | 命中 |
| 25 | C Awoke | private/1 | 9207 | 94 | 27 | 9112 | 95 | 2.54 s | 2.64 s | 27.0 | stop | 命中 |
| 26 | C 工具結果 1 | private/1 | 9512 | 278 | 77 | 9233 | 279 | 4.25 s | 4.35 s | 35.8 | tool_calls | 命中 |
| 27 | C 工具結果 1 | private/1 | 10930 | 1341 | 158 | 9589 | 1341 | 7.55 s | 7.65 s | 32.5 | tool_calls | 命中 |
| 28 | C 工具結果 1 | private/1 | 12473 | 1385 | 221 | 11088 | 1385 | 7.64 s | 7.76 s | 34.4 | tool_calls | 命中 |
| 29 | C 工具結果 1 | private/1 | 12863 | 169 | 147 | 12693 | 170 | 3.33 s | 3.45 s | 34.9 | tool_calls | 命中 |
| 30 | C 工具結果 1 | private/1 | 13137 | 127 | 43 | 13009 | 128 | 3.00 s | 3.12 s | 22.2 | stop | 命中 |
| 31 | C 工具結果 2 | private/1 | 13405 | 225 | 410 | 13180 | 225 | 3.57 s | 3.69 s | 30.4 | tool_calls | 命中 |
| 32 | C 工具結果 2 | private/1 | 14397 | 582 | 228 | 13814 | 583 | 4.99 s | 5.12 s | 34.3 | tool_calls | 命中 |
| 33 | C 工具結果 2 | private/1 | 14824 | 199 | 60 | 14625 | 199 | 3.56 s | 3.69 s | 20.7 | stop | 命中 |
| 34 | C 工具結果 3 | private/1 | 15006 | 122 | 86 | 14885 | 121 | 2.90 s | 3.03 s | 35.7 | tool_calls | 命中 |
| 35 | C 工具結果 3 | private/1 | 15252 | 160 | 184 | 15091 | 161 | 3.06 s | 3.18 s | 35.3 | tool_calls | 命中 |
| 36 | C 工具結果 3 | private/1 | 15607 | 171 | 7 | 15437 | 170 | 3.43 s | 3.57 s | 20.1 | stop | 命中 |
| 37 | C 工具結果 4 | private/1 | 15848 | 234 | 243 | 15613 | 235 | 3.78 s | 3.92 s | 35.7 | tool_calls | 命中 |
| 38 | C 工具結果 4 | private/1 | 16276 | 185 | 13 | 16090 | 186 | 3.36 s | 3.50 s | 21.4 | stop | 命中 |
| 39 | C 工具結果 5 | private/1 | 16458 | 169 | 126 | 16288 | 170 | 3.05 s | 3.18 s | 35.2 | tool_calls | 命中 |
| 40 | C 工具結果 5 | private/1 | 16688 | 104 | 15 | 16587 | 101 | 2.62 s | 2.75 s | 26.3 | stop | 命中 |
| 41 | C 工具結果 6 | private/1 | 16861 | 158 | 72 | 16702 | 159 | 3.37 s | 3.50 s | 34.2 | tool_calls | 命中 |
| 42 | C 工具結果 6 | private/1 | 18094 | 1161 | 1122 | 16932 | 1162 | 7.08 s | 7.21 s | 29.7 | tool_calls | 命中 |
| 43 | C 工具結果 6 | private/1 | 19345 | 129 | 353 | 19215 | 130 | 2.97 s | 3.13 s | 28.3 | tool_calls | 命中 |
| 44 | C 工具結果 6 | private/1 | 19795 | 97 | 180 | 19700 | 95 | 2.39 s | 2.54 s | 28.3 | tool_calls | 命中 |
| 45 | C 工具結果 6 | private/1 | 19993 | 18 | 110 | 19974 | 19 | 0.40 s | 0.54 s | 28.8 | stop | 命中 |
| 46 | C RS1 | private/1 | 20436 | 333 | 120 | 20103 | 333 | 4.39 s | 4.54 s | 24.5 | stop | 命中 |
