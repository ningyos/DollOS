# DollOS 接上 Strata：chat-completions 模型介面，對話只往後加

**Spec**：`docs/superpowers/specs/2026-10-17-doll-harness-design.md` §2、§3，§9 的 S4、S6 的 chat_tools 部分，以及 S5 的 ContextBuilder（只做 MindLoop 用的部分）（APPROVED）。跟 spec 不一樣的地方列在文末「Spec 修正」，這份 plan 不改 spec。
**日期**：2026-10-18；2026-10-19 加入「對話只往後加」（決定 3、6、7，Task 4 的固定工具清單，Task 8、Task 9）
**狀態**：APPROVED（使用者 2026-10-19）
**分支 / worktree**：DollOS `feat/model-adapter-chat`，`.worktrees/model-adapter-chat/`，從 `feat/gura-english-luxtts`（`4840d75`，最新的 code）開。Server 是 `http://100.110.158.36:8080`。

---

## 一句話

DollOS 現在只會打 llama.cpp 的 `/completion`，再加上 GBNF grammar。這台 server 兩樣都沒有，所以 Doll 現在完全連不上。這個 plan 把模型介面換成 `/v1/chat/completions` 加上原生 tool calling，而且它是**唯一的路徑**：舊路徑在這台 server 上跑不起來，最後整個刪掉。

同時把每段對話改成**只往後加**：每回合只加一則小的 user 訊息，前面的訊息和工具清單一個 byte 都不動，server 的 cache 才接得上。開始思考前的等待，從每回合整個重算的 8–14 秒，降到約 0.3–3 秒（看新訊息多長）。

---

## 查證後的現況

### Server（2026-10-18、10-19 實測）

- Strata 0.1.40，模型 `qwen3.8-flash-next-iq3_s`，context 131072，**只有 1 個 compute slot**，只收文字，沒有 API key。`/health` 和 `/v1/models` 今天又確認過一次。
- **舊路徑完全不能用**：`/completion`、`/v1/completions`、`/tokenize` 都回 404。chat 上的 `grammar` 欄位被默默忽略，連寫錯的 GBNF 都回 200。`response_format` 只是在 system prompt 裡加一段說明，不會真的限制輸出，還會讓 cache 失效。開機的 grammar probe（`kernel.py:1256` → `llm/grammar_probe.py`）打的是 `/completion`，所以 daemon 現在連開機都過不了。
- **原生工具可以用**：回傳 `message.tool_calls`，一次可以有好幾個。`arguments` 是 JSON 字串，整數、陣列、物件都正確。Server **不檢查** schema：enum 以外的值照樣傳回來，布林可能變成字串 `"True"`。`tool_choice: "required"` 會回錯的工具名稱，不能用。
- **串流順序固定**：空的 role chunk（prefill 之前就到，不能拿來算 TTFT）→ `reasoning_content` → `content` → `tool_calls`（第一段帶 `index`、`id`、名稱，之後只帶 `arguments` 的片段）→ 最後一段帶 `finish_reason`、`usage`、`timings`，然後是 `data: [DONE]`。同一個回覆裡，說的話一定在工具呼叫前面（模板規定工具呼叫之後不能再有文字）。`: keep-alive` 不是固定間隔，**prefill 期間一個都沒有**（實測 4.5 秒的 prefill），所以 read timeout 要蓋過整段 prefill 加排隊。
- **`tool` 訊息不帶名稱也不帶 id**：模板把連續的 `tool` 訊息併成一則 user 訊息，裡面是一串 `<tool_response>`，所以結果只靠**順序**對應到呼叫。`arguments` 用 JSON 字串原樣送回去可以（實測 200，cache 也命中）。連續兩則 user 訊息也照收（10-19 實測）。
- **`max_tokens` 包含思考**：在思考中用完，`content` 是空的，`finish_reason` 是 `"length"`。在工具呼叫中用完，串流已經送出半截的參數，那不是合法的 JSON。
- **思考上限**：每個 request 帶 `reasoning_budget_tokens` 就有效，串流和帶工具時都有效。到上限時，server 插一句固定的話（「I have thought about this long enough; time to give my answer.」），模型接著回答。換別的欄位名稱都被忽略。`reasoning_effort` 設 low 或 high 會在 system 最前面插一行，整個 cache 失效；不帶就是 medium，不插任何東西。
  - **觸發上限的那次呼叫，`timings` 不能拿來算命中**：server 插完那句話會再算一次 prompt，`cache_n`、`prompt_n`、`prompt_ms`（還有 `usage.cached_tokens`）描述的是那一次。實測 `prompt_tokens` 355、`cache_n` 370、`prompt_n` 0、`prompt_ms` 345，實際的 prefill 是 4.5 秒。判斷方法：`cache_n + prompt_n != prompt_tokens`；沒觸發時兩邊相等（實測十幾次都是）。
- **Cache（10-19 起開了 conversation cache）**：1 個在算的 slot，加上最多 4 段停放的對話；所有 client 共用，最久沒用的先被擠掉。
  - 只能從**存檔點**接續：上次生成的結尾、最近幾次 prompt 的結尾、累積約 2k token 之後的訊息邊界（例如夠長的 system 的結尾）。存檔點必須剛好是新 prompt 的前綴。
  - 所以**只往後加**的對話（上一次的 messages + 原封不動的 assistant 訊息 + tool 結果 + 新訊息），就算中間跑過別的對話，也接得回來。改了前面任何一則訊息，就退回它前面最後一個存檔點；改到 system 或工具清單（工具排在最前面），就整個重算。10-19 實測：第一則 user 訊息的時間改一個字，184 token 整個重算；工具換個順序，660 token 整個重算。
  - assistant 訊息要連 `reasoning_content`、`tool_calls`（`arguments` 原樣字串）一起送回去。模板保留**每一則** assistant 訊息的 reasoning，不只最後一則，所以第 N 次的 prompt 剛好是第 N+1 次的前綴（10-19 實測：連續三回合接回 79、121 token，等於上一次的 prompt 加生成；工具那一輪之後接回 500/522；思考上限觸發過的回覆也接得回，164/190）。被截短（打斷）或少了 reasoning，會退回上一次 prompt 的結尾，多 1–3 秒；就算原封不動，約 1/7 的情況也會這樣（模板會去掉 content 頭尾的空白）。
  - 同樣的工具和 system、換掉第一則 user 訊息（等於開新的一頁）也接得上：8 回合之後，接回 2,948/2,998 token，1.4 秒（10-19 實測）。前提是工具加 system 超過約 2k token，system 的結尾才有存檔點；system 很短的兩段對話只換最後一則 user 訊息，每次都整個重算。
- **速度**（server 的 `prompt_ms`）：
  - 冷 prefill 約 3 秒 + 每 token 約 3.5 ms（1.3k 7.3 秒、6.5k 26 秒）。
  - 命中之後看新加多少 token：≤30 個 0.3–0.6 秒；約 100 個 2.3 秒；約 240 個 3.1 秒；1.4k 個 7.7 秒，跟冷的差不多。接回停放的對話只慢一點（0.5–0.9 秒）。
  - 解碼 32–42 tok/s（有 MTP）。
  - 排在別的 client 後面時另外要等：實測 4–57 秒，別人冷算 63k 的 prompt 時等了 220 秒。
- **錯誤**：驗證錯誤在串流開始之前就回 HTTP 400 和 JSON；太長直接 400，不會截斷。client 關掉連線，server 立刻停止生成，所以打斷可以用。

### DollOS（`feat/gura-english-luxtts`）

- **只有三個地方呼叫模型**，都用 `kernel.py:421` 建的同一個 adapter：
  1. MindLoop 的每一種回合（`mind_loop.py:1604-1686`）。
  2. `run_tool_cascade`（`cascade/tool_loop.py:169`）。它經過 `run_agent`，給 workflow（`workflow.py:320`）、consolidation keeper（`consolidation.py:62`）、evolution keeper 和 skeptic（`evolution_keeper.py:158`、`:176`，`evolution_trigger.py:204`）用。
  3. 開機 probe。

  日記、反思、agenda、pulse 都是 MindLoop 的回合；`memory_writer` 和各個 observer 不呼叫模型。
- **MindLoop 把整個 prompt 當成一則 user 訊息**，system 是空的（`mind_loop.py:1628`）。它帶 grammar，不帶工具 schema；模型只在 scaffolding 的「# Tools available」看到工具名稱，參數的形狀全靠 grammar。
- **每回合整個重新渲染**：`render_mind` 每回合從 MindState 重建整個 user 區塊（`mind_loop.py:771-787`，實際樣本約 3.9k token）。每回合都會變的東西很多：`iter`、所有相對時間（「40s ago」）、STALE 標記、`[Recent perceptions]`（全域 20 則）和 `[Recent outputs]`（15 則）的滑動視窗。現在沒有 `[Now]`，她只在 `[Self pulse]`/`[Cognition]` 出現時看到時鐘。
- **工具集合每回合不一樣**：`_active_tool_registry`（`mind_loop.py:1155-1263`）至少分出 10 種：一般、主人回合多 `LearnName`、反思、日記、agenda、pulse、safe mode、外部、外部 DM。反思一定在沒有 channel 的桶裡（`reflection_observer.py:45`），所以外部回合加 `PinSelf` 的分支實際上碰不到。
- **對話的鍵現在就有**：`drain_grouped` 把沒有 channel 的 perception 放在同一個 `''` 桶（主人本機輸入、日記、agenda、pulse、反思、工具結果），每個 Discord channel 或 DM 一個桶（`perception_queue.py:85-89`）。MCP 每次 `talk()` 是一個新的 channel。
- **現在的重送規則會讓她變啞**：一輪結束之後，只有失敗、或 `Recall`、`PinSelf`、`SelfRevision` 的結果，才會再跑一輪（`mind_loop.py:92`、`:1517-1525`）。原生工具下，如果她先呼叫工具（例如只呼叫 `MoodTool`），她要等下一輪才會開口，可是照現在的規則，那一輪不會跑。
- **工具執行本來就會拒絕**：`dispatch_one`（`tool_loop.py:70`）只接受傳進去的 registry 裡有的名稱，並用 pydantic 驗證參數。enum 不對、`"True"` 這類 server 不擋的情況，由它擋。
- **現有的 schema 產生器丟掉巢狀型別**：`templates.py:43-81` 的 `_compact_schema` 不保留 `items` 和 `$defs`，所以 `WriteSchedule.entries`、`SpawnWorkflow.tasks` 變成沒有內容的 array。
- **依賴五行 think 的地方**：`REVIEW` → `recent_reviews` → `[Recent self-review]`（`mind_loop.py:1706-1719`、`mind_prompt.py:171-175`）；`_turn_think_chars` 用 `</think>` 切（`mind_loop.py:1432`）；`cascade_log._parse_think` 抓五個欄位。`MOOD` 那行本來就沒寫進 state，mood 只由 `MoodTool` 改。
- **Prompt 在教舊格式**：`scaffolding.jinja:56-131`（JSON 的 `<tool_call>`、五行 think）、`:155-162`（工具清單）、`mind_prompt.py:224`（「Output a JSON array of 0..N actions」）、`mind_prompt.py:564`，還有 `subagent_scaffolding.jinja`。Server 的模板自己會在 system 裡教 XML 格式的工具呼叫，兩套會打架。
- **`[Memory context]` 會塞進一整天的 transcript**：`transcripts/{date}.md` 沒有標題，整天被索引成一個 chunk（`fts_store.py:83-121`）；主人那行在查詢前就寫進去（`mind_loop.py:483-494`），所以主人回合幾乎都命中整天的紀錄。30 句對話之後，一個 hit 就 3,505 字。
- **沒人寫的區塊**：`[Active tasks]`、`[Pending]` 永遠是 `(none)`（`state.active_tasks`、`pending_events` 只有 TODO）。
- **perception 渲染的四個 bug**：ScheduledMoment 讀 `data['text']`，producer 寫的是 `intent`，所以排程回合的內容是空的（`kernel.py:1140` vs `mind_prompt.py:426`）；ToolResultArrived 不顯示 `output_id`，她沒辦法照 scaffolding 說的去 `ReadToolOutput`；UserSpoke 被切到 200 字；AgendaMoment 印成 `{}`。
- **token 帳算整個 prompt**：energy 的消耗（`mind_loop.py:879-884`）和 Cognition 的每日額度（`cognition.py:196-204`）都用 `prompt_tokens`，包括 cache 命中的部分。對話變長之後會多扣 3–4 倍。
- **Telemetry**：`LLMCallRecord.from_dict` 是 `cls(**d)`，新欄位要有預設值。`TurnLatencyRecord.ttft_ms` 一直是 None。`run_tool_cascade` 的呼叫都被記成 `cascade`。
- **Config**：`LLMConfig` 只收 `provider="llamacpp"`、`template="qwen3-thinking"`。`timeout_s=60` 不夠，冷 prefill 加上排在別人後面會超過。`max_concurrency=2`，可是 server 只有 1 個 slot。
- **測試基準**：1687 passed，只有已知的 torch 和 PortAudio 失敗。

---

## 這個 plan 的決定

1. **chat-completions 是唯一的路徑**。新的 adapter 是一般的 OpenAI 相容 client，llama-server 開 `--jinja` 也大致接得上（`reasoning_budget_tokens` 和 `timings` 是 Strata/llama.cpp 的欄位，別家 server 會忽略或不給），所以刪掉舊路徑只少了 grammar。沒有 fallback，也不在執行中切換。
2. **回合迴圈照 harness §1**：一輪裡有工具呼叫，就把結果送回去，再跑一輪。一輪沒有工具呼叫、到了 `MAX_SYNC_REFEED_PASSES`（8）、或被打斷，回合就結束。這修掉上面「變啞」的問題。Fire-and-forget 不變：`Shell`、`SpawnWorkflow`、`SpawnMonitor` 送回去的是 job 的回執，真正的結果之後照舊以 `ToolResultArrived` perception 回來，回合不會等它。
3. **工具：每段對話一份固定清單，執行時才綁定**。工具不改名、不合併，那是 harness §4 之後的事。schema 從 pydantic 產生、依名稱排序；給模型的是這段對話所有回合種類用得到的工具的聯集，整個 run 一個 byte 都不變。這一回合真正能用的，照舊由 `_active_tool_registry` 決定，在執行時擋。不這樣的話，回合種類一換（就連多一個 `LearnName`），排在 prompt 最前面的工具就變了，整段 cache 作廢。這是原本延後的問題 1，要修 harness §6.2、§6.4。
4. **思考**：原文存進 cascade_log 和 trace。`REVIEW`、`[Recent self-review]`、`MOOD` 那行都拿掉；mood 照舊只由 `MoodTool` 改。
5. **思考上限用 `reasoning_budget_tokens`**，也就是 harness §3.3 的做法 1，實測有效。先用一個設定值套在所有呼叫上；她自己選 low/mid/high，等有數字之後再做。永遠不送 `reasoning_effort`。
6. **對話只往後加**（harness S5 的 ContextBuilder，只做 MindLoop 用的部分）。每段對話一份 message list：固定的 system → 這一頁開頭的快照 → 每回合一則新的 user 訊息（時間、變了的東西、這批 perception）→ 原封不動的 assistant 和 tool 訊息。前面的訊息永遠不重新渲染。對話太長（24k token）、固定前綴要變（current_self 採用了新版本）、daemon 重開、或上一次回了 400 時，開新的一頁（epoch），開頭是程式寫的快照，不用 LLM 摘要。排程不改。
7. **token 帳只算新加的部分**：energy 和每日額度改算這次呼叫新加的 prompt token，不算整段對話。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。Task 3 到 Task 5 之間，還沒換過去的地方暫時用舊的 adapter（它對 Strata 本來就跑不動，所以沒有退步），Task 7 刪掉。Task 4 到 Task 8 之間，MindLoop 照舊每回合整個重新渲染，Task 9 才換成只往後加。Task 1 要連 server 錄幾個小 fixture；其他除了 Task 11，都不需要 server。

### Task 0 — worktree 和基準

- `git worktree add .worktrees/model-adapter-chat -b feat/model-adapter-chat feat/gura-english-luxtts`，然後 `uv sync`。
- 驗收：`uv run pytest -q` 跑完，**記下基準**。預期是 1687 passed，加上已知的 torch/PortAudio 失敗；不一樣就列給你看，不在這裡修。

### Task 1 — `ModelAdapter` 介面和 chat-completions 實作

- 檔案：新增 `src/dollos/llm/model.py`（事件和介面）、`src/dollos/llm/chat_completions.py`；修改 `src/dollos/telemetry/llm_calls.py`。
- 事件（frozen dataclass）：
  - `ThinkDelta(text)`、`SpeechDelta(text)`。
  - `ToolCalls(calls)`：每個 `ToolCall` 有 `id`、`name`、`arguments`（原始的 JSON 字串）。
  - `End`：`finish_reason`、`usage`、`assistant_message`、`ttft_ms`、`dropped_tool_calls`、`think_capped`。`usage` 有 `prompt_tokens`、`completion_tokens`、`cache_n`、`prompt_n`、`prompt_ms`、`predicted_per_second`、`draft_n`、`draft_n_accepted`；server 沒給的就是 None。
  - `think_capped`：`cache_n + prompt_n != prompt_tokens` 時為 True（思考上限觸發了，見上面）。這時 `cache_n`、`prompt_n`、`prompt_ms` 一律記成 None，因為那不是這個 prompt 的數字。
- 介面：`stream(*, messages, tools, max_tokens, think_budget, purpose, on_usage, conversation=None, prior_tokens=0)` 回傳事件的 async iterator；`check()` 是開機檢查。`conversation` 是 `(鍵, 第幾頁)`，只拿來記錄；`prior_tokens` 是同一段對話上一次呼叫的 `prompt_tokens + completion_tokens`（Task 9 才開始傳）。
- Request：`model`、`messages`、`tools`（空的就不送）、`stream: true`、`max_tokens = max_tokens + think_budget`、`reasoning_budget_tokens = think_budget`、`presence_penalty: 1.3`（跟現在一樣）。**不送** `grammar`、`response_format`、`tool_choice`、`reasoning_effort`。`api_key` 有設才送 `Authorization: Bearer`。
- 解析串流：
  - 跳過空行和 `:` 開頭的行；忽略空的 role chunk 和空字串的片段；`choices` 是空的 chunk（只帶 usage）也要接受。
  - `reasoning_content` → `ThinkDelta`；`content` → `SpeechDelta`，不管它出現在工具片段之前還是之後。
  - `tool_calls` 依 `index` 組起來：`id` 和名稱取第一段，`arguments` 一段段接起來（參數會在任何位置被切開，例如 `"text":` 和值分在兩段）。
  - 最後一段取 `finish_reason`、`usage`、`timings`；看到 `[DONE]` 結束。
  - `ModelError` 帶 `kind`，之後 mind-loop 的 `brain_down` 用得到：
    - `transport`：連不上、逾時、串流開始前的 5xx（例如載入模型時的 503；mind-loop §8 把它算成 brain_down）、串流在 `finish_reason` 或 `[DONE]` 之前就斷了。
    - `server`：串流開始前的 4xx（訊息裡帶 server 回的原文，例如太長）、chunk 裡有 `error`。
- `ToolCalls` 在串流結束後只送一次：
  - `finish_reason == "tool_calls"`：送出全部。
  - `"length"`：這個回覆的工具呼叫**全部丟掉**（最後一個一定是半截的 JSON，前面的也不執行，規則簡單），數量記在 `End.dropped_tool_calls`，並寫 warning。
  - 其他 `finish_reason` 卻有工具片段：`ModelError(kind="server")`，不猜。
- `assistant_message` 是下一輪要原封不動送回去的訊息：`role`、`content`、`reasoning_content`（有才放）、`tool_calls`（`id`、`type: "function"`、名稱、收到的 `arguments` 字串原樣）。另外提供 `tool_message(call_id, text)` 產生 `role: "tool"` 的訊息。
- TTFT 從第一個不是空的思考、文字或工具片段算起，不從 role chunk 算。
- Telemetry：每次呼叫在 `finally` 寫一筆 `LLMCallRecord`，出錯或被打斷也寫。新增欄位，都有預設值 None：`finish_reason`、`cache_n`、`prompt_n`、`prompt_ms`、`predicted_per_second`、`first_content_ms`、`think_capped`、`conversation`、`epoch`、`new_prompt_tokens`。
- `on_usage(new_prompt_tokens, completion_tokens)`，`new_prompt_tokens = max(prompt_tokens − prior_tokens, 0)`。`prior_tokens` 預設 0，所以 Task 9 之前跟現在一樣。這個數字不靠 server 的 cache，思考上限觸發時也對。不用 `usage.cached_tokens`，它可能比 `prompt_tokens` 還大。
- 所有呼叫共用一個 `asyncio.Semaphore(max_concurrency)`。httpx 的 timeout：connect 5 秒，read 用 `timeout_s`。
- `check()`：`GET /health` 的 `status` 要是 `"ok"`；`GET /v1/models` 裡要有設定的 model。不符合就丟 `RuntimeError`，訊息寫出 URL、設定的 model、server 實際回了什麼。用短的 timeout（10 秒），不用 `timeout_s`。**不送生成 request**，因為那會擠掉 cache。
- 測試（`tests/test_llm_chat_completions.py`，用 respx）：SSE fixture 用小 request 從這台 server 錄（`max_tokens` ≤ 200），存在 `tests/fixtures/strata/`。error chunk 錄不到，那一個手寫。
  - 思考 → 文字 → 兩個工具呼叫：事件順序對；`ToolCalls` 有 2 個，參數接得完整；`End` 的 usage 和 timings 都有值，`think_capped` 是 False。
  - keep-alive 行和 role chunk 被忽略；TTFT 不是從 role chunk 算的。
  - 在工具呼叫中被 `length` 截斷（錄法：`max_tokens` 70、`reasoning_budget_tokens` 16，請她直接呼叫 NoteMemory 寫 60 個字）：沒有 `ToolCalls`，`dropped_tool_calls == 1`。在思考中被截斷：`End` 是 `length`，沒有 `SpeechDelta`。
  - 思考上限觸發的 fixture：`think_capped` 是 True，`cache_n`、`prompt_n`、`prompt_ms` 是 None。
  - 工具片段之後又來 `content`（手改 fixture）：照樣變成 `SpeechDelta`，`ToolCalls` 仍在最後。
  - 串流前的 400 → `ModelError(kind="server")`，帶 server 的訊息；串流前的 503 → `transport`；串流中的 error chunk → `server`；串流在 `[DONE]` 之前斷掉 → `transport`；`finish_reason` 是 `stop` 卻有工具片段 → `server`。
  - Request body 有 `reasoning_budget_tokens`、`max_tokens` 是兩者相加；沒有 `grammar`、`tool_choice`、`reasoning_effort`；沒有工具時不送 `tools`。
  - `prior_tokens`：`on_usage` 收到扣掉之後的數字，不會是負的；`LLMCallRecord` 記下 `conversation`、`epoch`、`new_prompt_tokens`。
  - 提早關掉 iterator：HTTP 連線被關掉，`on_usage` 和 `LLMCallRecord` 照樣有寫（取代 `test_stream_close_propagation.py` 要保護的行為）。
  - task 在等 semaphore 時、或串流到一半時被 cancel：semaphore 放掉、連線關掉。只有 1 個 slot，主人輸入取消 consolidation 時靠這個讓出位置。
  - `check()`：正常、model 不對、health 不是 ok、連不上，各一個。
  - `LLMCallRecord.from_dict` 讀得了沒有新欄位的舊資料。

### Task 2 — 從 pydantic 產生工具 schema

- 檔案：新增 `src/dollos/llm/tool_schema.py`，提供 `tool_schemas(tool_classes) -> list[dict]`。
- 改法：
  - 每個工具產生 `{"type": "function", "function": {"name": cls.__name__, "description": docstring, "parameters": ...}}`。
  - `parameters` 從 `model_json_schema()` 來：把 `$ref`/`$defs` 展開，拿掉 `title`；保留 `type`、`enum`、`anyOf`、`items`、`properties`、`required`、`default`、`description`、`minimum`、`maximum`。
  - 依名稱排序。同一組工具不管傳進來的順序，產生的 JSON 一個 byte 都不差，因為它就是 cache 前綴的開頭。
  - 不改名、不合併工具。
- 測試（`tests/test_tool_schema.py`）：
  - `tools.py` 裡每一個工具類別（含 `LearnName`、`PinSelf`、`SelfRevision`、`Report`）都產生得出 schema，裡面沒有 `$ref` 也沒有 `title`。
  - `WriteSchedule.entries` 和 `SpawnWorkflow.tasks` 的 `items` 有 `properties`。
  - `Scratchpad.op`、`Report.status` 的 enum 還在；`Recall.since` 的 `anyOf` 還在。
  - 同一組工具換順序傳進去，`json.dumps` 的結果相同。
  - `dispatch_one` 拒絕 enum 以外的值（例如 `Scratchpad.op="delete"`）。這條測試記錄「server 不擋的，`dispatch_one` 擋」。（現在的工具沒有布林欄位，`"True"` 的情況不用測。）

### Task 3 — config 和開機檢查

- 檔案：`src/dollos/config.py`、`src/dollos/kernel.py`、`config.example.toml`、`scripts/persona_stability_smoke.py:37`（只改說明文字）。
- `[llm]` 改成：
  - `provider: Literal["openai-chat"] = "openai-chat"`
  - `base_url`
  - `model`（取代 `model_alias`；它就是 `/v1/models` 裡的 id）
  - `api_key: str = ""`
  - `timeout_s = 300`（冷 prefill 加上排在別人後面；keep-alive 不是固定送的）
  - `max_concurrency = 1`（server 只有 1 個 slot）
  - `think_budget_tokens = 1024`
  - 拿掉 `template`。`extra="forbid"` 不變，所以舊的 `config.toml` 會在載入時直接報錯，指出哪個欄位不認得。這是故意的。
- kernel：
  - 建 `self.model = ChatCompletionsAdapter(...)`。
  - 開機時在原本 grammar probe 的位置（IPC 起來之前）改成 `await self.model.check()`，失敗就不開機。
  - `kernel.py:608` 的 `model_id` 改讀 `settings.llm.model`。
  - 舊的 `self.adapter` 暫時保留，給還沒換的 MindLoop 和 agent 用；`build_adapter` 拿掉 provider/template 的判斷，直接建。
- `config.example.toml`：`[llm]` 改成 Strata 的 URL 和 model id，註解寫上 1 個 slot、思考上限的意思。
- 測試：
  - `tests/test_config.py`：新的 provider、預設值；`template` 或 `model_alias` 被拒。
  - `tests/test_kernel.py`：monkeypatch 的對象從 `assert_bounded_repetition_supported`（第 579、720、842、971 行）改成新的 check；第 1005 行起那條「probe 在 IPC 之前跑」的測試改成新的 check：丟錯時 IPC 不會起來。
  - 約 16 個測試檔裡的 `LLMConfig(provider="llamacpp", template=..., model_alias=...)` 機械式改成新寫法，行為不變。

### Task 4 — MindLoop 改用 ModelAdapter

- 檔案：`src/dollos/mind/mind_loop.py`、`mind_prompt.py`、`mind_state.py`、`trace.py`、`src/dollos/cascade_log.py`、`src/dollos/telemetry/turn_latency.py`、`src/dollos/cascade/tool_loop.py`（共用的小函式）、`src/dollos/kernel.py`（把 `self.model` 傳給 MindLoop）、`src/dollos/workflow.py`（只改 cascade_log callback，見下）。
- 訊息：
  - `[system: self._system_prompt_for_turn(), user: render_mind(...)]`（Task 9 換成只往後加的對話）。
  - `render_mind` 的 `system_prompt` 改成可以不給，不給時就不輸出開頭那段；其他區塊的順序不變。
- 工具（決定 3）：
  - `offered = self._offered_tools()`：只看 tier 的固定清單，跟回合種類、safe mode 無關。`tools = tool_schemas(offered.values())`。

    | tier | offered |
    |---|---|
    | internal | `MAIN_TOOLS` + `NoteToolLesson` + `LearnName`；self_profile 開著加 `PinSelf`，evolution 開著加 `SelfRevision` |
    | external_dm（主人的 DM） | `EXTERNAL_TOOLS` + `LearnName` |
    | external_public | `EXTERNAL_TOOLS` |

  - `bound = self._active_tool_registry()`，內容不變（回合種類、safe mode、主人在場才有 `LearnName`）。safe mode 從此只在執行時擋。
- 每一輪：`max_tokens=1024`（說話加工具的額度，adapter 會再加上思考額度）；`think_budget = settings.llm.think_budget_tokens`；`on_usage` 跟現在一樣。
- 事件怎麼處理：
  - `ThinkDelta` → 累積思考原文（算 think_chars、寫 trace）。
  - `SpeechDelta` → `SentenceChunker` → `_emit_sentence`。這個出口不變：空白句不送、agenda 和日記回合不出聲、`first_speak_ms` 在這裡量。
  - `ToolCalls` → 串流結束後，先 flush chunker（最後一句先說出去），再照 index 順序執行。執行步驟：`json.loads(arguments)`，失敗就當成工具失敗，回「參數不是合法的 JSON」；然後 `dispatch_one(name, args, ctx, bound)`。`_dispatch_tool` 原本的工具記憶和 action log 不變。
  - 在 `offered` 裡但不在 `bound` 裡：回 tool 訊息「`<Tool>` is not available in this turn (`<kind>`); available now: `<這回合綁定的工具>`」，action log 記 `rejected_unbound`，**不算失敗**（不進 safe mode 的連續失敗、不寫 `tool_stats` 的失敗），但算進回合內「同一工具連三次」的上限。agenda、日記、pulse、safe mode 的回合現在看得到 `Shell` 這些工具的 schema，沒有這條上限，她可能把 8 輪都花在被拒的呼叫上。不在 `offered` 裡的名稱照舊被拒、算失敗。
  - 解析參數、照順序執行、產生 tool 訊息這段，寫成 `cascade/tool_loop.py` 裡的一個小函式，Task 5 也用它。
- 結果送回去：
  - 把 `End.assistant_message` 原封不動加進 messages。
  - 每個呼叫加一則 `tool` 訊息，順序跟 `tool_calls` 一樣（模板只靠順序對應）：成功就放結果，結果是 None 就放 `"ok"`，失敗就放錯誤說明。
- 迴圈：
  - 這一輪有工具呼叫才繼續。
  - 保留：`Recall` 之後設 `external_ctx`；同一工具連三次失敗、連續失敗進 safe mode；`MAX_SYNC_REFEED_PASSES = 8`；每個邊界都檢查打斷。
  - 刪掉：`FIRE_AND_FORGET_TOOLS`、`IN_TURN_REFEED_TOOLS`，還有 grammar 的建立和快取（`_grammar`、`_grammar_cache`、`_active_grammar`）。MindLoop 不再因為 grammar 在開機時丟錯。
- `finish_reason == "length"`：寫 warning（帶 turn_id）。已經說出去的話就算數，這個回覆的工具呼叫都不執行（adapter 已經丟掉了），回合結束。
- 思考：
  - 刪掉 `_capture_review` 和 `[Recent self-review]`。`MindState.recent_reviews` 也刪掉；載入舊的 state 檔時忽略那個 key。
  - think_chars 改用 `ThinkDelta` 的長度。
  - cascade_log：用 `reasoning`（原文，最多 4000 字）和 `speech` 取代五個欄位，刪掉 `_parse_think`；`log_iter` 改收 `reasoning`、`speech`。workflow 的 callback（`workflow.py:343-358`）也呼叫 `log_iter`，但 Task 5 之前還是舊路徑：這個 task 先在 callback 裡用 `</think>` 把舊的原文切成兩段，Task 5 換掉。
  - trace：`raw_assistant_emit` 換成完整的 `reasoning`、`speech`，以及帶 `id` 的 `tool_calls`。
- `TurnLatencyRecord`：`ttft_ms` 用第一輪的 `End.ttft_ms`。新增 `cache_n`、`prompt_n`、`finish_reason`，取第一輪的值，預設 None。
- 測試：
  - 新增 `tests/_fake_model.py`：`FakeModel` 實作 `stream()`，記下每次呼叫的 messages 和 tools，記的是**呼叫當下的 JSON 快照**，不是 list 的參照（記參照的話，前面的訊息被改了也看不出來，「前綴」測試永遠會過）。
    - 每個預先寫好的回覆可以是事件清單，也可以是現在那種原始文字。原始文字會被轉成事件：`</think>` 之前是 `ThinkDelta`；`<tool_call>{json}</tool_call>` 在最後變成 `ToolCalls`，id 自動產生；其餘是 `SpeechDelta`。
    - 回覆用完之後，回傳空的回覆（沒有話、沒有工具），回合就會結束。這跟現在 `_FakeLLM` 的 pass ≥ 2 一樣；新規則下有工具呼叫就一定會有第二輪，沒有這條，大部分現有測試會跑出界。
    - 這樣大部分現有測試只要換掉假模型。這個 task 只換 MindLoop 的：`test_mind_loop.py:14-47` 的 `_FakeLLM` 和 `_mindloop_factory.py`。`_dispatcher_helpers.py` 的 `_FakeAdapter`（沒有測試在用）和 `_HangAdapter`（`test_workflow.py` 在用）都繼承 `LLMAdapter`，留給 Task 5 處理。
  - 新增：
    - 說的話在任何工具執行之前就到 sink。
    - 一個回覆兩個工具呼叫：照順序執行，產生兩則 `tool` 訊息，`tool_call_id` 對得上。
    - 第二輪的 messages 開頭和第一輪一個 byte 都不差，而且 assistant 訊息裡有 `reasoning_content`。
    - internal 的 tools JSON 在一般、主人、反思、日記、agenda、pulse 回合和 safe mode 下一個 byte 都不差；external 的沒有任何內部工具。
    - 呼叫有提供但這回合沒綁定的工具（一般回合呼叫 `PinSelf`）：被拒、說明送回去（列出這回合能用的工具）、trace 看得到、不算失敗；同一個工具被拒三次，回合結束。呼叫清單外的名稱：被拒、算失敗。
    - `Shell` 的回執送回去，回合不等 job。
    - 第一輪只呼叫 `MoodTool`：會跑第二輪，第二輪說的話有送到 sink（變啞的情況）。
    - `length`：什麼都不執行。
    - 回合最多 8 輪。
    - 連續兩個回合的 system 訊息完全相同。
    - think_chars 來自 `ThinkDelta`。
  - 改寫（它們釘住舊規則）：
    - `test_mind_loop.py`、`test_mind_loop_evolution_wiring.py`、`test_mind_loop_self_profile.py`、`test_mind_loop_trace.py` 裡斷言 `<tool_response>`、`IN_TURN_REFEED_TOOLS` 或每回合工具集合的測試，改成 `tool` 訊息和新規則。
    - `test_cascade_log.py` 改成檢查 `reasoning` 欄位。
    - `test_trace.py`（`raw_assistant_emit`）和 `test_mind_loop_turn_latency.py:48`（現在斷言 `ttft_ms is None`）跟著改。
  - 刪掉：
    - `test_core_loop_robustness_gate.py` 的 `test_p2_grammar_tool_precedes_review`、`test_p2_capture_recent_reviews_field_and_block`。
    - `test_mind_state_durability.py`、`test_mind_prompt.py` 裡 `recent_reviews` 的測試。

### Task 5 — agent 迴圈改用 ModelAdapter

- 檔案：`src/dollos/cascade/tool_loop.py`、`src/dollos/agent_engine.py`、`src/dollos/workflow.py`、`src/dollos/mind/consolidation.py`、`evolution_keeper.py`、`evolution_trigger.py`、`src/dollos/kernel.py`。
- 改法：
  - `run_tool_cascade(model, system, messages, tools, ..., purpose)` 不再收 grammar。system 放成第一則訊息。事件處理和結果送回的方式跟 Task 4 一樣，用同一個小函式。
  - `agent_engine.py` 拿掉 `build_qwen3_think_tool_grammar`。
  - 迴圈：呼叫了 `Report` 就結束（不變）；這一輪有工具呼叫才繼續；連三次失敗的規則不變。新增 `max_passes=16`：現在沒有上限，但在新規則下，一個一直不 Report 的 agent 會停不下來。
  - `purpose` 分別傳 `workflow`、`consolidation`、`evolution`，順便修掉現在全部被記成 `cascade` 的問題。
  - `prior_tokens` 傳上一輪的 `prompt_tokens + completion_tokens`（agent 的 messages 本來就只往後加），每日額度只算新加的部分。
  - 思考額度用同一個設定值；各 agent 現有的 `max_tokens`（workflow 4096、consolidation 和 evolution 2048）意思變成回答的額度。
  - workflow 的 cascade_log callback 拿掉 Task 4 的 `</think>` 暫時切法，直接傳 `reasoning`、`speech`。
  - kernel 把 `self.model` 傳給 WorkflowRunner、ConsolidationTrigger、EvolutionTrigger。
- 測試：
  - `test_tool_loop.py`（7）、`test_workflow.py`（23）、`test_consolidation.py`（17）、`test_evolution_keeper.py`、`test_evolution_trigger.py` 改用 `FakeModel`；斷言 `<tool_response>` 的地方改成 `tool` 訊息。
  - `_dispatcher_helpers.py`：`_HangAdapter` 改成會卡住的 `FakeModel`，`_FakeAdapter` 刪掉。之後 `tests/` 裡就沒有東西 import `LLMAdapter`，Task 7 才刪得掉。
  - 新增：`purpose` 有寫進 `LLMCallRecord`；16 輪上限；一個回覆裡同時有 `Report` 和別的工具時，那一輪做完就結束；第二輪起 `prior_tokens` 是上一輪的總數。

### Task 6 — Prompt 不再教舊格式

- 檔案：`src/dollos/prompts/templates/scaffolding.jinja`、`subagent_scaffolding.jinja`、`src/dollos/mind/mind_prompt.py`、`src/dollos/kernel.py`（`split_scaffolding` 不再傳 `tool_registry`；`tools_doc` 是 `prompts/renderer.py` 從它算出來的）。
- `scaffolding.jinja`：
  - 刪掉「# Format」「# Think structure」「# Tools available」（schema 已經帶著每個工具的說明），以及所有 `<think>` 和 JSON `<tool_call>` 的寫法。
  - 「# Output」改寫：
    - 回覆的文字會被唸出來；思考不會被聽到；工具呼叫也不會被聽到。
    - 同一個回覆裡，說的話在工具呼叫前面（這是協定的事實）；工具結果回來之後，可以再說，也可以不說。
    - 「說做了不等於做了」那段保留，以 `NoteMemory` 為例。
  - 範例改成白話描述，不寫任何格式：主人問早餐的事，她說「Okay, noted. Breakfast tomorrow.」，並呼叫 `NoteMemory`，text 是「主人要我記得：明天要吃早餐」。工具呼叫的格式由 server 的模板教。
- `mind_prompt.py`：`[Decision time]` 拿掉「Output a JSON array of 0..N actions」；`:564` 的「(no text outside tool_call)」改成「(say nothing)」。
- `subagent_scaffolding.jinja`：刪掉「# Think structure」和 `<think>`/`<tool_call>` 那幾句；Report 的規則保留；寫明她的文字不會給任何人看到，要做事就呼叫工具，最後用 Report 結束。
- 測試：`tests/test_prompt_renderer.py`：渲染結果沒有 `<tool_call>`、沒有 `SEEN:`；Gura 那條「Okay, noted」照過。`tests/test_mind_prompt.py:295` 跟著改。

### Task 7 — 刪掉舊路徑

- 刪掉：`llm/transport.py`、`llm/composed.py`、`llm/templates.py`、`llm/grammar_probe.py`、`llm/adapter.py`、`tool_parser.py`、`stream_events.py`（只有 `tool_parser` 和舊的 MindLoop 用它）、kernel 的 `self.adapter`/`build_adapter`/`_build_template`/`_MindLLMAdapter`；`llm/__init__.py` 改成只匯出新的東西。
- 刪掉的測試（共 112 個，都只測舊的線上格式）：
  - `test_llm_transport.py`（11）、`test_transport_on_usage.py`（4）
  - `test_llm_templates.py`（22）、`test_llm_grammar.py`（31）、`test_voice_first_grammar.py`（4）
  - `test_grammar_probe.py`（2）、`test_llm_composed.py`（5）
  - `test_stream_close_propagation.py`（3，它要保護的行為已由 Task 1 接手）
  - `test_tool_parser.py`（17）、`test_voice_first_parser.py`（11）、`test_stream_events.py`（2）
- 驗收：`grep -rn "/completion\|grammar\|ToolStreamParser\|StreamChunk" src/` 沒有結果；`grep -rn "dollos.llm.adapter\|stream_events" src/ tests/` 沒有結果；測試數 = 基準 − 112 + 新測試，全部通過。

### Task 8 — 每回合只送新的東西：快照和增量的渲染

- 檔案：`src/dollos/mind/mind_prompt.py`、`mind_loop.py`（`_derive_memory_hits`）、`src/dollos/tools.py`（`Recall`）、`src/dollos/cascade/tool_loop.py`（Task 4 的小函式）。
- 新增兩個純函式，用現有的區塊函式（`_render_memory`、`_percep_body`、`energy_bucket_line`、`render_body_signal` 等）組起來。`render_mind` 先不動，MindLoop 還在用，Task 9 才換。每段對話帶一份 `Seen`：每個快照區塊上次送出的文字 hash，以及這一頁已經出現過的 hit（`chunk_hash` 或 id）。
- `render_epoch_start(state, seen, tier, now)`：一頁開頭的快照，一則 user 訊息：
  ```
  [New conversation page — 14:05 Mon 10-19. Earlier context is not shown; use Recall for details.]
  [Self profile] …
  [Mind state] mood (reason) · focus · energy     ← 沒有 iter，沒有「40s ago」
  [Your agenda] / [Open loops]                     ← 絕對日期，拿掉 STALE
  [Scratchpad] …
  [External situation] …                           ← 只有 external_public
  [Recent perceptions] 這段對話自己的，舊的在前，「HH:MM Kind: body」
  [Recent outputs] 「HH:MM …」                     ← 只有 internal 和 external_dm（同現在）
  ```
  internal 另外帶當下的 `[Self pulse]`/`[Cognition]`（由呼叫的人傳進來，見 Task 9）；safe mode 開著時加一行。external_public 的開頭那行不寫「use Recall for details」，因為那裡的 Recall 只搜 `external_public/`，找不到頻道裡的舊對話。recap 不含這回合自己的 perception（deque 在渲染前就加進去了）。
- `render_turn_delta(state, seen, tier, perceptions, hits, events, now)`：每回合一則 user 訊息，依序：
  1. `[14:05 Mon 10-19]`
  2. 文字變了的快照區塊（Self profile、Mind state、agenda、open loops、scratchpad），整塊重送。別的對話或背景工作改的也算。
  3. 事件區塊：`[Self pulse]`/`[Cognition]`（只有 internal；本來就是變了才出現，由呼叫的人傳進來）；反思回合的 `[Tool outcomes since last reflection]` 和 `[人格演化候選]`（`surface_or_expire` 照現在每個反思回合呼叫）；pulse 回合的 `[Body signal]`；safe mode 跟這段對話上次看到的不一樣時一行（開始或解除，帶原因）。safe mode 是全域的，所以狀態記在每段對話的 `Seen` 裡，不是「剛好在變的那個回合」才說。
  4. 這一頁還沒出現過的 `[Memory context]`/`[Associative memories]`/`[Tool habits]` hit。
  5. 這批的每個 perception 一行，例如「14:04 Owner (local text): …」，全文。
- 兩個函式都不再有：`[Active tasks]`、`[Pending]`、`[Tool notes]`（失敗的結果已經是 tool 訊息）、`[Decision time]`、`iter`、所有相對時間、「<30s just spoke」警告、一直掛著的 `[Safe mode]` 橫幅。重複的警告照舊由 RepeatLoopDetected perception 送。
- external_public 照 mind-loop §10 外部那一列：mood 只有 emotion；沒有 focus、mood reason、agenda、open loops、scratchpad、pulse、cognition；沒有記憶自動注入和 recent outputs（同現在）；`[Self profile]` 留著（P1e 已接受）。
- perception 那一行修掉四個 bug：ScheduledMoment 讀 `data['intent']`；ToolResultArrived 顯示 `output_id` / `details_output_id`；UserSpoke 不再切到 200 字；AgendaMoment 寫成一句話（「time for your own goals; nothing you say now is heard」）。`_percep_body` 是共用的，所以現在的 `render_mind` 也跟著修好。
- 記憶自動注入（現在的 `render_mind` 也跟著變）：
  - 查詢用這回合自己的輸入，不用全域最新的 UserSpoke。
  - 排除 `transcripts/`；`Recall` 照樣搜得到。
  - 每回合最多 3 個新的 hit，每個最多 300 字。`ctx.turn_memory_sources` 改記她 context 裡真的有的 hit：這回合新給的，加上這一頁之前給過、這回合又查到的。PursueGoal 的 provenance 靠它，不能記她沒看到的（今天查到的 hit 全部都會渲染，所以兩者一樣；有了 3 個的上限之後就不一樣了）。
  - S1（外部記憶隔離）改的是同一段：誰後 merge，誰保留 harness §6.5 第 1 點（主人和 internal 回合不自動注入 `external_public/`、`external_dm/`）。
- 工具結果會一直留在對話裡，所以要變小：
  - `Recall` 每個 hit 最多 500 字，取查詢詞在內容裡第一次出現的地方前後；都沒出現才從開頭取。transcript 一天是一個 chunk，從開頭取的話，新的一頁要找的「今天稍早的對話」永遠拿不到，而開頭那行正是叫她用 Recall 找。
  - 任何一則 tool 訊息超過 8,000 字就切掉，加上「(cut; N chars total)」。`InvokeSkill` 不切：skill 是她要照著做的全文，沒辦法分頁讀。
- 測試（`tests/test_turn_delta.py`）：
  - 快照區塊只有文字變了才重送；hit 一頁只出現一次；每回合最多 3 個、每個 ≤ 300 字。
  - `transcripts/` 的 hit 不會被自動注入，`Recall` 仍找得到。
  - recap 不含這回合的 perception，也只含這段對話自己的 channel。
  - 兩個函式自己寫的文字裡沒有相對時間、沒有 `iter`（傳進來的 `[Self pulse]` 有自己的時間戳，不算）；每行 perception 都有 `HH:MM`。
  - external_public 的輸出只有 §10 允許的區塊，開頭那行沒有叫她 Recall。
  - safe mode：在別段對話開始的，這段對話下一次的 delta 也有那一行；解除時同樣。
  - 四個 bug 各一條，用真的 producer payload（例如 `intent`）；現有測試裡用 `data={"text": ...}` 造 ScheduledMoment、又斷言渲染結果的，改成 `intent`。
  - Recall：hit 很長、查詢詞在後半段時，500 字裡有那個詞；tool 訊息 8,000 字的上限，`InvokeSkill` 不切。
  - `turn_memory_sources` 不含被 3 個上限擋掉的 hit。
  - 現有測試裡斷言 `render_mind` 的 `[Memory context]`、UserSpoke 200 字、Recall 全文的地方，跟著新規則改（開工時列出來）。

### Task 9 — ContextBuilder：對話只往後加

- 檔案：新增 `src/dollos/mind/context_builder.py`；修改 `mind_loop.py`、`mind_prompt.py`（刪掉 `render_mind` 的組合）、`scaffolding.jinja`、`trace.py`、`src/dollos/telemetry/turn_latency.py`、`src/dollos/perception/cognition.py`、`src/dollos/perception/system_pulse.py`、`src/dollos/config.py`、`config.example.toml`。
- **對話的鍵**：`private` = 現在的 `''` 桶；`ch:<channel_id>` = 每個 channel 一段（主人的 DM 是 external_dm，其他是 external_public）。等 mind-loop 的 places 做好，鍵改成 place（home 和 inner 共用 `private`）。daemon 記憶體裡最多留 8 段 channel 對話，最久沒用的先丟（`private` 不丟）；丟掉的下次用時開新的一頁。
- **每段對話**存：固定前綴的 hash（工具 JSON + system）、第幾頁、messages、`Seen`、上一次呼叫的 `prompt_tokens + completion_tokens`。
- **system** = `_system_prompt_for_turn()` + `[Memory guideline]`（從 user 區塊搬過來）。整個 run 不變；Task 6 之後約 2.9k token，超過約 2k，所以 system 的結尾是一個存檔點。
- **每回合**：
  - 開始時檢查要不要開新的一頁（下面）。新的一頁是 system + `render_epoch_start`，`prior_tokens` 從 0 算。
  - 加一則 `render_turn_delta`。
  - 每一輪把 `End.assistant_message` 原封不動加進去，再加 tool 訊息（Task 4 的做法），下一輪送整份。
  - `stream(conversation=(鍵, 第幾頁), prior_tokens=上一次的總數)`。這次呼叫沒有 usage（出錯、被打斷）時，「上一次的總數」維持上一次有 usage 的值。
- **身體和認知區塊**：`SystemPulse.snapshot()`、`CognitionWorker.snapshot()` 是「變了才給一次」，現在每個回合都呼叫（`mind_loop.py:661-672`），不管是誰的回合。改成只在組 `private` 的訊息時呼叫；不然外部回合會把變化吃掉，`private` 永遠看不到。新的一頁用新增的 `current_block()`：照最新的樣本渲染，並記下已經送出，下一次只在又變了時才給。
- **開新的一頁**只有四種情況（log 寫原因）：
  1. 固定前綴的 hash 變了（current_self 採用了新版本）。
  2. 上一次的 `prompt_tokens + completion_tokens` ≥ `[llm].conversation_budget_tokens`（新設定，預設 24000，必須 > 0）。server 沒有 `/tokenize`，所以用上一次的數字。
  3. 記憶體裡沒有這段對話（daemon 剛開，或被丟掉了）。
  4. 上一次呼叫回了 `server` 錯誤（4xx，例如太長）。壞掉或太長的訊息會一直留在對話裡，不換頁的話之後每次都錯。

  回合種類、safe mode、閒置多久，都**不會**開新的一頁。24k 的理由：命中時長度幾乎不影響速度（6.8k 和 63k 的對話接回都在 0.5–2.6 秒）；被擠掉時最壞約 87 秒，一頁大約能放 15–25 個回合。
- **打斷和錯誤**：
  - 主人在串流中打斷：把到目前為止的 assistant 訊息加進去（已有的思考 + 已經交給 sink 的句子，沒有工具呼叫），紀錄跟實際說出去的一樣。下一次會退回上一個 prompt 結尾，多 1–3 秒一次。還沒有任何輸出就被打斷：什麼都不加。
  - 工具呼叫回來之後才被打斷：每個呼叫都要有 tool 訊息，沒執行的寫「not run: turn interrupted」。
  - `transport` 錯誤：還沒有任何輸出時，那則 user 訊息留著，下回合再加一則 user 訊息（server 收連續兩則 user 訊息沒問題）；已經有輸出時，跟打斷一樣處理。
  - `server` 錯誤：下回合開新的一頁（上面第 4 種）。
  - `finish_reason == "length"`：加 adapter 的 `assistant_message`（工具呼叫已經丟掉）。
- **日記**：純 DiaryMoment 的回合跑成一次性的 request：`[system, user(這一刻的快照 + [Today's log] + DiaryMoment 那行)]`。快照就是 internal 的 `render_epoch_start`，但不帶 `[Self pulse]`/`[Cognition]`（不然日記會把「變了」吃掉，`private` 看不到）；今天的日記回合看得到 `[Self profile]`、心情、agenda，不能少。用 internal 的工具清單（所以前綴從 `private` 接回，約 1.4 秒），綁定照舊是 `{WriteDiary, Recall}`。不存，不碰 `private`。`[Today's log]` 有 11–30k token，要 40–105 秒；它在背景、不出聲。
- **token 帳**：energy 用 `on_usage` 收到的新 token（Task 1）。Cognition 的每日額度加總 `new_prompt_tokens`；升級當天的舊紀錄沒有這個欄位，照舊算 `prompt_tokens`。「mind room」照舊看最後一次的 `prompt_tokens`，意思變成對話長度。
- **trace**：每一輪照舊存新加的 messages 全文（`input_messages_delta`），從這段對話上一次記錄的位置算起，所以被打斷那輪補上的 assistant 訊息也記得到（被取消的那輪本身不寫 trace）；另外記 `conversation`、`epoch`、index 範圍。每一頁一筆 `epoch_start`（工具 hash、system hash、快照全文），夠重建完整的 context。取代「pass 0 存整個渲染好的 prompt」。`TurnLatencyRecord` 加 `conversation`、`epoch`。
- **scaffolding**：「# Perception blocks」「# Memory」改寫成新格式：每回合一則新訊息、開頭是時間、hit 只在第一次出現時給。刪掉 `[Active monitors]`、`[Pending events]` 這兩個從來沒渲染過的說明。
- 測試（`tests/test_context_builder.py`，用 `FakeModel`）：
  - 前綴：同一段對話裡，第 N 次 request 的 messages 是第 N+1 次的 byte 前綴，跨輪、也跨不同種類的回合（主人 → agenda → 反思 → 主人）。
  - 新的一頁：超過預算時；current_self 換了時；daemon 重開後第一回合；上一次回 400 時。快照不重複這回合的 perception。
  - 錯誤：`transport` 錯誤後 user 訊息留著，下回合接著加；400 之後下回合是新的一頁。
  - 打斷：部分的 assistant 訊息，加上「not run」的 tool 訊息；還沒輸出就被打斷，什麼都不加。
  - 日記：一次性、有快照、internal 的工具清單、`private` 的 messages 沒變。
  - 隔離：`private` 和 channel 的對話之間，內容不會互相跑進去。
  - 身體和認知：`ch:` 回合不呼叫 `snapshot()`；在 `ch:` 回合期間發生的變化，`private` 下一回合看得到。
  - LRU：第 9 段 channel 對話進來時丟掉最久沒用的；`private` 不會被丟；被丟的下次開新的一頁。
  - 帳：energy 用新的 token；每日額度用 `new_prompt_tokens`。
  - trace：用 `epoch_start` 加上每輪的範圍，重建得出實際送出去的 messages。
- 改寫和刪掉：`tests/test_mind_prompt*.py` 裡釘住 `render_mind` 全文和順序的組合測試刪掉或改寫（開工時列出清單給你看）；區塊函式的測試留著。mind_loop、energy、diary、pulse、evolution 測試裡斷言 user 區塊內容的地方，改成看 delta。

### Task 10 — 文件

- 檔案：`CLAUDE.md`。
- 改法：第 15、63、220-225 行改成：模型介面是 `/v1/chat/completions` 加原生工具，沒有 grammar；server 是 Strata（URL、1 個 slot、只收文字）；思考上限用 `reasoning_budget_tokens`；每段對話只往後加、一份固定工具清單（執行時綁定）、`conversation_budget_tokens`。第 5、153、258 行還寫著 llama.cpp / `model_id`，一起改。
- 測試：無，只改文件。

### Task 11 — 對真的 server 跑一次，記下第一批數字

- 準備：
  - `config.toml` 指向 Strata，語音和 Discord 關掉，`[data] root` 指到一個新的空目錄，不碰真的記憶。`[trace] root` 不跟著 `[data] root`（預設 `data/traces`），也指到同一個新目錄。
  - 先看 `/metrics` 的 `live` 和 `conversation_cache`（restores、evictions）：有別人在用的話記下來，因為那會影響數字。
- 開機：
  - `uv run dollos --config config.toml`，檢查通過。
  - 把 `model` 改錯再開一次：要拒絕開機，錯誤訊息寫出設定的 model 和 server 實際的 model。
- 用 `experiments/ws_client.py` 跑 5 個回合：
  1. 「Hi Gura, what are you up to?」（新的一頁）
  2. 緊接著：「Tell me one thing you like about the ocean.」
  3. 「Remember that my dentist appointment is Friday at 3.」→ 要呼叫 `NoteMemory`、有說話、資料目錄裡有那則筆記。
  4. 「What do I have on Friday?」→ 呼叫 `Recall`，第二輪的回答用到結果。
  5. 「Remind me Thursday evening about the dentist.」→ 呼叫 `WriteSchedule`，巢狀的 `entries` 正確，排程有存起來。

  中間插兩個別的對話：
  - 第 2、3 回合之間：smoke script 直接對 server 送一個不相干的小 request（別的 system），模擬別的 client。
  - 第 3、4 回合之間：smoke script 用 IPC 的 `ChannelRegister`/`ChannelEvent`（MCP server 用的那套）送一則外部訊息，讓一段 `ch:` 對話跑一回合。
- 再跑兩次：
  - 換頁：`conversation_budget_tokens` 設成比第 1 回合的 `prompt_tokens` 多約 2k，跑到出現新的一頁，再多跑一回合。
  - 重開：重開 daemon，送一個回合。
- 判斷命中（只看 `think_capped` 是 False 的呼叫）：`prompt_n ≤ new_prompt_tokens + 64` 是命中；`≤ new_prompt_tokens + 上一次的 completion_tokens + 64` 是退回上一個 prompt 結尾；其他是沒命中。
- 過關：
  - 5 個回合都沒有錯誤，回覆是英文；唸出來的文字裡沒有 `<tool_call>`、`SEEN:` 這類東西。
  - **命中**：第 2–5 回合和每個多輪回合的第二輪，沒有一次是「沒命中」（退回可以，記下次數）。新訊息 ≤ 50 token 的命中回合，server `prompt_ms` < 1 秒。
  - **跨對話接回**：第 3、4 回合排在別的 request、別的對話後面，照樣命中。
  - **換頁**：新的一頁第一次呼叫的 `cache_n` 約等於固定前綴的大小，約 5–9 秒；下一回合又命中。
  - **重開**：開了新的一頁，記下時間（前綴還停在 server 上就接得回 system 的結尾）。
- 記下數字（從 `llm_calls`、`turn_latency` 的 JSONL 和 `/metrics`）：
  - 每個回合：`conversation`、`epoch`、`prompt_tokens`、`new_prompt_tokens`、`cache_n`、`prompt_n`、`prompt_ms`、TTFT、`first_speak_ms`、think_chars、解碼 tok/s、輪數、`finish_reason`、`think_capped`、扣了多少 energy。
  - 彙總：命中和沒命中的 TTFT、命中回合的 `first_speak_ms`（主要是思考）、命中和退回的比例、換頁和重開的代價、思考上限觸發的比例、解碼速度，以及這段時間的 `conversation_cache` restores/evictions 和別的 client。
- 寫成一頁 `docs/research/2026-10-18-strata-first-numbers.md`，給你看。
- 之後：Gura 英文 plan 卡住的 Task 6 smoke（`scripts/persona_stability_smoke.py`）也能跑了。

---

## 已知代價（先量，這個 plan 不修）

- **開口前的等待，大部分變成思考**。Task 9 之後預期如下（server 端，Task 11 量實際的值）：

  | 情況 | 第一個 token 之前 |
  |---|---|
  | 命中，新訊息 ≤ 約 30 token | 0.3–0.5 秒（在 slot 裡）；0.8–0.9 秒（從停放的對話接回） |
  | 命中，新訊息 50–250 token（有記憶 hit、區塊變了） | 1.4–3.1 秒 |
  | 第二輪：短的工具結果 / `Recall` 大小的結果 | 0.3–0.9 秒 / 2.5–4 秒 |
  | 新的一頁或 daemon 重開，前綴還停在 server 上 | 約 5–9 秒，每頁一次 |
  | 新的一頁，前綴也被擠掉了 | 約 33 秒 |
  | 被擠掉的對話，接近 24k 預算 | 最多約 87 秒 |
  | 日記（一次性，背景、不出聲） | 40–105 秒 |

  之後還要思考：33 tok/s 下 300 token 約 9 秒，這是剩下最大的一塊，修法是她自己選思考額度（之後）。現在每回合重算 3–4k 的區塊，思考前就要 8–14 秒。
- **Cache 還是會被擠掉**。換回合種類不會了（固定工具清單），但 server 只停放 4 段對話，所有 client 共用，最久沒用的先丟：外部聊天、背景 agent（一個 4 個 worker 的 workflow 就能全部擠掉）、這台 server 的其他使用者。`private` 最常跑，通常還停著。被擠掉的對話下回合整個重算（上表）。這個 plan 只記錄（`conversation`、`epoch`、`new_prompt_tokens`、`prompt_n`），不改排程。
- **新的一頁看不到上一頁的原文**。快照是程式寫的：self profile、狀態、agenda、這段對話最近的 perception 和輸出；更早的細節她要用 `Recall`（公開頻道裡做不到，那裡的 Recall 只搜 `external_public/`）。daemon 重開也一樣，因為對話紀錄只在記憶體裡（從 turn journal 重建等 mind-loop M3）。
- **私人對話和主人的 Discord 私訊互相看不到對方的近況**。今天兩邊都看得到全域最近 20 則 perception；之後每段對話只看自己的。Q1（mind-loop §17）決定之後再調。
- **排隊**：compute slot 只有 1 個、大家共用。主人的回合要排在已經在算的呼叫後面，實測多等 4–57 秒，別人冷算長 prompt 時要幾分鐘；ContextBuilder 解決不了。背景 agent 也一個一個跑：consolidation 120 秒、evolution 240 秒、workflow 每個 agent 300 秒的上限沒改，容易逾時（consolidation 和 evolution 會被主人輸入取消，workflow 不會）。Task 11 不跑這些，之後看 `llm_calls` 的 `purpose` 再說。
- **大的工具結果**：`Recall` 這種幾百 token 的結果，下一輪要多 2–4 秒，而且會一直留在這一頁（Task 8 只截短）。
- 原本的兩個問題：固定工具清單由這版決定（決定 3）；`--conversation-cache-mib` 已在 2026-10-19 打開。

---

## Spec 修正（列出來，跟這個 plan 一起審；spec 檔案之後另外改）

- **harness §9 S4**：「先包住現有的 `/completion` + grammar 路徑」在 Strata 上不能跑。S4 改成直接做 chat 的 ModelAdapter，並且跟 S6 的 chat_tools 部分一起提前，排在 S2、S3、S5 前面。§4 的工具合併仍留在之後。
- **harness §9 S5、§2**：ContextBuilder 提前到這個 plan，只做 MindLoop 用的部分：每段對話一份只往後加的 message list，前面的不重新渲染；太長或前綴要變時開新的一頁，開頭是程式寫的快照。ToolRegistry、ToolExecutor 抽出和單一的 Turn Runner 仍留在 S5。
- **harness §6.2 第 2 點、§6.4**：「沒綁定的工具不會出現在給模型的工具清單裡」「每個回合只把這回合的工具 schema 交給模型」改成：每段對話（之後是每個 place）一份固定的 schema 清單，是它的 principal 在這個 place 各種回合可能被綁定的工具的聯集；ToolExecutor 在執行時落實這回合的綁定（TurnPolicy ∩ 矩陣）。principal 那一欄以外的工具永遠不提供，所以陌生人看不到內部工具。
- **harness §9 S6、S8、S9**：`[harness].model_api` 的開關和兩種 `model_api` 的 A/B，在這台 server 上沒有意義，因為只有一條路徑跑得起來。S9「移除其中一條路徑」由這個 plan 的 Task 7 做掉。
- **harness §3.1**：「llama.cpp `--jinja`」改成「任何 OpenAI 相容的 server」，現在用的是 Strata。
- **harness §3.3**：選做法 1（`reasoning_budget_tokens`，已實測）；做法 2 不做。
- **mind-loop §5**：「跟回合共用 transport 的 `Semaphore(2)`」改成 `max_concurrency = 1`（server 只有 1 個 slot）。另外記下：server 只停放 4 段對話、跟所有 client 共用，挑選規則之後要考慮（例如背景 agent 別插在主人的兩個回合之間）。
- **mind-loop §8**：「KV prompt cache 會失去」改成：daemon 的對話紀錄會失去，重開後每段對話開新的一頁；server 上前綴相同的 entry 可能還在，接得回 system 的結尾。
- **mind-loop §10**：可見性表「外部 place」那一列、「`[Memory context]` 的查詢改用這回合自己的輸入」，先在 Task 8 的新渲染函式裡做。`[Self pulse]`/`[Cognition]` 只在私人的對話渲染。另外兩點跟表不一樣：home 和 inner 共用一段對話，所以彼此看得到全部，不只是「尾巴」；Q1 決定之前，主人私訊和私人對話互相看不到對方的近況（比 Q1 兩案都嚴）。
- **mind-loop §6、§11（M6）**：回合被退回 inbox 時，ContextBuilder 要撤掉這回合加進對話的訊息（只撤尾巴，前面不動），不然重跑時同樣的輸入會出現兩次。今天被取消的輸入算已消費，所以這個 plan 不需要。
- **mind-loop §9、代謝 vital spec（2026-07-10）**：energy 公式 `(completion + 0.25·prompt)` 裡的 prompt 改成這次呼叫新加的 prompt token；Cognition 的每日額度也一樣（決定 7）。不然對話變長之後，每回合多扣 3–4 倍。spec 把典型回合校準在約 0.05，改了之後會變小；Task 11 記下數字後再決定要不要調 `token_per_energy_unit`。
- **think A/B spec**：Arm A（grammar + 格式化 think）在這台 server 上不能跑。redesign §15 Q4（B 組要不要跑）也就沒有意義了。
- **redesign R1a**：「在現行 grammar 路徑上接 Qwen3.8」由這個 plan 取代。**R1b**：「量 think A/B Arm A 基準」改成 Task 11 在 chat 路徑上量的數字。

---

## 驗收

- Task 0 記錄的基準，扣掉 Task 7 列出的 112 個舊測試和 Task 9 列出的 `render_mind` 組合測試，加上新測試，全部通過。除此之外沒有刪任何測試。
- `src/` 裡沒有 `/completion`、`grammar`、`ToolStreamParser`。
- 測試保證：同一段對話裡，每次 request 的 messages 都是下一次的前綴；internal 的工具 JSON 和 system 在整個 run 裡不變。
- daemon 對 Strata 開得起來；model 設錯時拒絕開機，錯誤訊息清楚。
- Task 11 過關（5 個回合、命中、跨對話接回、換頁、重開），數字已經寫好並給你看。

## 不在範圍內

- 工具合併成約 15 個（harness §4）、TurnPolicy（S3）、ToolRegistry/ToolExecutor 抽出和單一 Turn Runner（S5）、MCP client（S7）、places（mind-loop M1）。
- 她自己選 low/mid/high 的思考額度；背景 LLM 呼叫的排程和 slot 的分配。
- 對話紀錄存檔（重開後從 turn journal 重建，等 mind-loop M3）、LLM 摘要、預熱 request、閒置後換頁、滑動截掉舊訊息、拿掉舊的 reasoning、每段對話各自的 safe mode、harness §8 的 output store（Task 8 只截短）。
- 圖片（這台 server 不收）、Anthropic adapter、assistant prefill、server 端的設定。
- 打斷的實機測試（`ws_client.py` 不會送打斷；行為由 Task 1 和 Task 9 的單元測試保護）。
