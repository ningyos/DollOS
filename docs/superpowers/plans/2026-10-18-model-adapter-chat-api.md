# DollOS 接上 Strata：chat-completions 模型介面

**Spec**：`docs/superpowers/specs/2026-10-17-doll-harness-design.md` §2、§3，§9 的 S4 和 S6 的 chat_tools 部分（APPROVED）。跟 spec 不一樣的地方列在文末「Spec 修正」，這份 plan 不改 spec。
**日期**：2026-10-18
**狀態**：待使用者審
**分支 / worktree**：DollOS `feat/model-adapter-chat`，`.worktrees/model-adapter-chat/`，從 `feat/gura-english-luxtts`（`4840d75`，最新的 code）開。Server 是 `http://100.110.158.36:8080`。

---

## 一句話

DollOS 現在只會打 llama.cpp 的 `/completion`，再加上 GBNF grammar。這台 server 兩樣都沒有，所以 Doll 現在完全連不上。這個 plan 把模型介面換成 `/v1/chat/completions` 加上原生 tool calling，而且它是**唯一的路徑**：舊路徑在這台 server 上跑不起來，最後整個刪掉。

---

## 查證後的現況

### Server（2026-10-18 實測）

- Strata 0.1.40，模型 `qwen3.8-flash-next-iq3_s`，context 131072，**只有 1 個 slot**，只收文字，沒有 API key。`/health` 和 `/v1/models` 今天又確認過一次。
- **舊路徑完全不能用**：`/completion`、`/v1/completions`、`/tokenize` 都回 404。chat 上的 `grammar` 欄位被默默忽略，連寫錯的 GBNF 都回 200。`response_format` 只是在 system prompt 裡加一段說明，不會真的限制輸出，還會讓 cache 失效。開機的 grammar probe（`kernel.py:1256` → `llm/grammar_probe.py`）打的是 `/completion`，所以 daemon 現在連開機都過不了。
- **原生工具可以用**：回傳 `message.tool_calls`，一次可以有好幾個。`arguments` 是 JSON 字串，整數、陣列、物件都正確。Server **不檢查** schema：enum 以外的值照樣傳回來，布林可能變成字串 `"True"`。`tool_choice: "required"` 會回錯的工具名稱，不能用。
- **串流順序固定**：
  1. 一個空的 role chunk。它在 prefill 之前就到，不能拿來算 TTFT。
  2. `reasoning_content`。
  3. `content`。
  4. `tool_calls`：第一段帶 `index`、`id`、名稱，之後只帶 `arguments` 的片段。
  5. 最後一段帶 `finish_reason`、`usage`、`timings`，然後是 `data: [DONE]`。

  中間偶爾有 `: keep-alive`，但不是固定間隔：**prefill 期間一個都沒有**（實測 4.5 秒的 prefill，第一個 keep-alive 在第一個 token 前才到），所以 read timeout 要蓋過整段 prefill 加排隊。同一個回覆裡，說的話一定在工具呼叫前面；模板規定工具呼叫之後不能再有文字。
- **`tool` 訊息不帶名稱也不帶 id**：模板把連續的 `tool` 訊息併成一則 user 訊息，裡面是一串 `<tool_response>`，所以結果只靠**順序**對應到呼叫。`arguments` 用 JSON 字串原樣送回去可以（實測 200，cache 也命中）。
- **`max_tokens` 包含思考**：在思考中用完，`content` 是空的，`finish_reason` 是 `"length"`。在工具呼叫中用完，串流已經送出半截的參數，那不是合法的 JSON。
- **思考上限**：每個 request 帶 `reasoning_budget_tokens` 就有效，串流和帶工具時都有效。到上限時，server 插一句固定的話（「I have thought about this long enough; time to give my answer.」），模型接著回答。換別的欄位名稱都被忽略。`reasoning_effort` 設 low 或 high 會在 system 最前面插一行，整個 cache 失效；不帶就是 medium，不插任何東西。
  - **觸發上限的那次呼叫，`timings` 不能拿來算命中**：server 插完那句話會再算一次 prompt，`cache_n`、`prompt_n`、`prompt_ms`（還有 `usage.cached_tokens`）描述的是那一次。實測 `prompt_tokens` 355、`cache_n` 370、`prompt_n` 0、`prompt_ms` 345，實際的 prefill 是 4.5 秒。判斷方法：`cache_n + prompt_n != prompt_tokens`；沒觸發時兩邊相等（實測十幾次都是）。
- **Cache 只有一條，只能從固定的點接續**：上一個 request 最後一則訊息的結尾、上次生成的結尾、累積約 2k token 後的訊息邊界。
  - 改了最後一則 user 訊息，就退回 system 的結尾（system 要超過約 2k token 才有這個點）。
  - 工具定義排在整個 prompt 的最前面，所以**工具集合一變，就整個重算**。
  - assistant 訊息（含 `reasoning_content`）原封不動送回去，下一輪連生成的部分都命中。
  - 別的 client 的 request 也會把 cache 擠掉，探測時就發生過好幾次。
- **速度**：
  - 冷的 prefill 約 2.5 秒起跳，之後每 1k token 約 3 秒（實測 513 token 3.8 秒、2.5k 10.3 秒、3.3k 12.6 秒）。
  - 命中之後只多算幾十個 token，TTFT 約 0.3 秒；**多算幾百個 token 就要幾秒**（命中 2.5k、新加 372 token：3.6 秒）。
  - 解碼 32–42 tok/s（有 MTP）。
- **錯誤**：驗證錯誤在串流開始之前就回 HTTP 400 和 JSON；太長直接 400，不會截斷。client 關掉連線，server 立刻停止生成，所以打斷可以用。

### DollOS（`feat/gura-english-luxtts`）

- **只有三個地方呼叫模型**，都用 `kernel.py:421` 建的同一個 adapter：
  1. MindLoop 的每一種回合（`mind_loop.py:1604-1686`）。
  2. `run_tool_cascade`（`cascade/tool_loop.py:169`）。它經過 `run_agent`，給 workflow（`workflow.py:320`）、consolidation keeper（`consolidation.py:62`）、evolution keeper 和 skeptic（`evolution_keeper.py:158`、`:176`，`evolution_trigger.py:204`）用。
  3. 開機 probe。

  日記、反思、agenda、pulse 都是 MindLoop 的回合；`memory_writer` 和各個 observer 不呼叫模型。
- **MindLoop 把整個 prompt 當成一則 user 訊息**，system 是空的（`mind_loop.py:1628`）。它帶 grammar，不帶工具 schema；模型只在 scaffolding 的「# Tools available」看到工具名稱，參數的形狀全靠 grammar。
- **現在的重送規則會讓她變啞**：一輪結束之後，只有失敗、或 `Recall`、`PinSelf`、`SelfRevision` 的結果，才會再跑一輪（`mind_loop.py:92`、`:1517-1525`）。原生工具下，如果她先呼叫工具（例如只呼叫 `MoodTool`），她要等下一輪才會開口，可是照現在的規則，那一輪不會跑。
- **工具執行本來就會拒絕**：`dispatch_one`（`tool_loop.py:70`）只接受傳進去的 registry 裡有的名稱，並用 pydantic 驗證參數。enum 不對、`"True"` 這類 server 不擋的情況，由它擋。
- **現有的 schema 產生器丟掉巢狀型別**：`templates.py:43-81` 的 `_compact_schema` 不保留 `items` 和 `$defs`，所以 `WriteSchedule.entries`、`SpawnWorkflow.tasks` 變成沒有內容的 array。
- **依賴五行 think 的地方**：
  - `REVIEW` → `recent_reviews` → `[Recent self-review]`（`mind_loop.py:1706-1719`、`mind_prompt.py:171-175`）。
  - `_turn_think_chars` 用 `</think>` 切（`mind_loop.py:1432`）。
  - `cascade_log._parse_think` 抓五個欄位。
  - `MOOD` 那行本來就沒寫進 state，mood 只由 `MoodTool` 改。
- **Prompt 在教舊格式**：`scaffolding.jinja:56-131`（JSON 的 `<tool_call>`、五行 think）、`:155-162`（工具清單）、`mind_prompt.py:224`（「Output a JSON array of 0..N actions」）、`mind_prompt.py:564`，還有 `subagent_scaffolding.jinja`。Server 的模板自己會在 system 裡教 XML 格式的工具呼叫，兩套會打架。
- **Telemetry**：`LLMCallRecord.from_dict` 是 `cls(**d)`，新欄位要有預設值。`TurnLatencyRecord.ttft_ms` 一直是 None。`run_tool_cascade` 的呼叫都被記成 `cascade`。
- **Config**：`LLMConfig` 只收 `provider="llamacpp"`、`template="qwen3-thinking"`。`timeout_s=60` 不夠，冷 prefill 加上排在別人後面會超過。`max_concurrency=2`，可是 server 只有 1 個 slot。
- **測試基準**：1687 passed，只有已知的 torch 和 PortAudio 失敗。

---

## 這個 plan 的決定

1. **chat-completions 是唯一的路徑**。新的 adapter 是一般的 OpenAI 相容 client，llama-server 開 `--jinja` 也大致接得上（`reasoning_budget_tokens` 和 `timings` 是 Strata/llama.cpp 的欄位，別家 server 會忽略或不給），所以刪掉舊路徑只少了 grammar。沒有 fallback，也不在執行中切換。
2. **回合迴圈照 harness §1**：一輪裡有工具呼叫，就把結果送回去，再跑一輪。一輪沒有工具呼叫、到了 `MAX_SYNC_REFEED_PASSES`（8）、或被打斷，回合就結束。這修掉上面「變啞」的問題。Fire-and-forget 不變：`Shell`、`SpawnWorkflow`、`SpawnMonitor` 送回去的是 job 的回執，真正的結果之後照舊以 `ToolResultArrived` perception 回來，回合不會等它。
3. **工具不改名、不合併**，那是 harness §4 之後的事。每回合的工具集合照舊由 `_active_tool_registry` 決定，schema 從 pydantic 產生；執行時只接受這一輪交給模型的工具。
4. **思考**：原文存進 cascade_log 和 trace。`REVIEW`、`[Recent self-review]`、`MOOD` 那行都拿掉；mood 照舊只由 `MoodTool` 改。
5. **思考上限用 `reasoning_budget_tokens`**，也就是 harness §3.3 的做法 1，實測有效。先用一個設定值套在所有呼叫上；她自己選 low/mid/high，等有數字之後再做。永遠不送 `reasoning_effort`。
6. **Prompt 拆成兩則訊息**：system 是整個 run 都不變的 system prompt（身份、current_self、scaffolding）；user 是每回合會變的區塊。在 Strata 上，user 訊息裡的區塊怎麼排都一樣（cache 只會退回 system 的結尾），所以不重排。Cache 和延遲先量，不改排程。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。Task 3 到 Task 5 之間，還沒換過去的地方暫時用舊的 adapter（它對 Strata 本來就跑不動，所以沒有退步），Task 7 刪掉。Task 1 要連 server 錄幾個小 fixture；其他除了 Task 9，都不需要 server。

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
- 介面：`stream(*, messages, tools, max_tokens, think_budget, purpose, on_usage)` 回傳事件的 async iterator；`check()` 是開機檢查。
- Request：`model`、`messages`、`tools`（空的就不送）、`stream: true`、`max_tokens = max_tokens + think_budget`、`reasoning_budget_tokens = think_budget`、`presence_penalty: 1.3`（跟現在一樣）。**不送** `grammar`、`response_format`、`tool_choice`、`reasoning_effort`。`api_key` 有設才送 `Authorization: Bearer`。
- 解析串流：
  - 跳過空行和 `:` 開頭的行；忽略空的 role chunk 和空字串的片段；`choices` 是空的 chunk（只帶 usage）也要接受。
  - `reasoning_content` → `ThinkDelta`；`content` → `SpeechDelta`，不管它出現在工具片段之前還是之後。
  - `tool_calls` 依 `index` 組起來：`id` 和名稱取第一段，`arguments` 一段段接起來（參數會在任何位置被切開，例如 `"text":` 和值分在兩段）。
  - 最後一段取 `finish_reason`、`usage`、`timings`；看到 `[DONE]` 結束。
  - `ModelError` 帶 `kind`，之後 mind-loop 的 `brain_down` 用得到：
    - `transport`：連不上、逾時、串流在 `finish_reason` 或 `[DONE]` 之前就斷了。
    - `server`：串流開始前的 4xx/5xx（訊息裡帶 server 回的原文）、chunk 裡有 `error`。
- `ToolCalls` 在串流結束後只送一次：
  - `finish_reason == "tool_calls"`：送出全部。
  - `"length"`：這個回覆的工具呼叫**全部丟掉**（最後一個一定是半截的 JSON，前面的也不執行，規則簡單），數量記在 `End.dropped_tool_calls`，並寫 warning。
  - 其他 `finish_reason` 卻有工具片段：`ModelError(kind="server")`，不猜。
- `assistant_message` 是下一輪要原封不動送回去的訊息：`role`、`content`、`reasoning_content`（有才放）、`tool_calls`（`id`、`type: "function"`、名稱、收到的 `arguments` 字串原樣）。另外提供 `tool_message(call_id, text)` 產生 `role: "tool"` 的訊息。
- TTFT 從第一個不是空的思考、文字或工具片段算起，不從 role chunk 算。
- Telemetry：每次呼叫在 `finally` 寫一筆 `LLMCallRecord`，出錯或被打斷也寫。新增欄位，都有預設值 None：`finish_reason`、`cache_n`、`prompt_n`、`prompt_ms`、`predicted_per_second`、`first_content_ms`、`think_capped`。`on_usage(prompt_tokens, completion_tokens)` 跟現在一樣呼叫。不用 `usage.cached_tokens`，它可能比 `prompt_tokens` 還大。
- 所有呼叫共用一個 `asyncio.Semaphore(max_concurrency)`。httpx 的 timeout：connect 5 秒，read 用 `timeout_s`。
- `check()`：`GET /health` 的 `status` 要是 `"ok"`；`GET /v1/models` 裡要有設定的 model。不符合就丟 `RuntimeError`，訊息寫出 URL、設定的 model、server 實際回了什麼。用短的 timeout（10 秒），不用 `timeout_s`。**不送生成 request**，因為那會擠掉 cache。
- 測試（`tests/test_llm_chat_completions.py`，用 respx）：SSE fixture 用小 request 從這台 server 錄（`max_tokens` ≤ 200），存在 `tests/fixtures/strata/`。error chunk 錄不到，那一個手寫。
  - 思考 → 文字 → 兩個工具呼叫：事件順序對；`ToolCalls` 有 2 個，參數接得完整；`End` 的 usage 和 timings 都有值，`think_capped` 是 False。
  - keep-alive 行和 role chunk 被忽略；TTFT 不是從 role chunk 算的。
  - 在工具呼叫中被 `length` 截斷（錄法：`max_tokens` 70、`reasoning_budget_tokens` 16，請她直接呼叫 NoteMemory 寫 60 個字）：沒有 `ToolCalls`，`dropped_tool_calls == 1`。在思考中被截斷：`End` 是 `length`，沒有 `SpeechDelta`。
  - 思考上限觸發的 fixture：`think_capped` 是 True，`cache_n`、`prompt_n`、`prompt_ms` 是 None。
  - 工具片段之後又來 `content`（手改 fixture）：照樣變成 `SpeechDelta`，`ToolCalls` 仍在最後。
  - 串流前的 400 → `ModelError(kind="server")`，帶 server 的訊息；串流中的 error chunk → `server`；串流在 `[DONE]` 之前斷掉 → `transport`；`finish_reason` 是 `stop` 卻有工具片段 → `server`。
  - Request body 有 `reasoning_budget_tokens`、`max_tokens` 是兩者相加；沒有 `grammar`、`tool_choice`、`reasoning_effort`；沒有工具時不送 `tools`。
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
  - `[system: self._system_prompt_for_turn(), user: render_mind(...)]`。
  - `render_mind` 的 `system_prompt` 改成可以不給，不給時就不輸出開頭那段；其他區塊的順序不變。
- 每一輪：
  - `offered = self._active_tool_registry()`，只算一次。
  - `tools = tool_schemas(offered.values())`。
  - `max_tokens=1024`：這是說話加工具的額度，adapter 會再加上思考額度。
  - `think_budget = settings.llm.think_budget_tokens`；`on_usage` 跟現在一樣。
- 事件怎麼處理：
  - `ThinkDelta` → 累積思考原文（算 think_chars、寫 trace）。
  - `SpeechDelta` → `SentenceChunker` → `_emit_sentence`。這個出口不變：空白句不送、agenda 和日記回合不出聲、`first_speak_ms` 在這裡量。
  - `ToolCalls` → 串流結束後，先 flush chunker（最後一句先說出去），再照 index 順序執行。執行步驟：`json.loads(arguments)`，失敗就當成工具失敗，回「參數不是合法的 JSON」；然後 `dispatch_one(name, args, ctx, offered)`，不在 `offered` 裡的名稱會被拒絕。`_dispatch_tool` 原本的工具記憶和 action log 不變。
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
  - 新增 `tests/_fake_model.py`：`FakeModel` 實作 `stream()`，記下每次呼叫的 messages 和 tools。
    - 每個預先寫好的回覆可以是事件清單，也可以是現在那種原始文字。原始文字會被轉成事件：`</think>` 之前是 `ThinkDelta`；`<tool_call>{json}</tool_call>` 在最後變成 `ToolCalls`，id 自動產生；其餘是 `SpeechDelta`。
    - 回覆用完之後，回傳空的回覆（沒有話、沒有工具），回合就會結束。這跟現在 `_FakeLLM` 的 pass ≥ 2 一樣；新規則下有工具呼叫就一定會有第二輪，沒有這條，大部分現有測試會跑出界。
    - 這樣大部分現有測試只要換掉假模型。這個 task 只換 MindLoop 的：`test_mind_loop.py:14-47` 的 `_FakeLLM` 和 `_mindloop_factory.py`。`_dispatcher_helpers.py` 的 `_FakeAdapter`（沒有測試在用）和 `_HangAdapter`（`test_workflow.py` 在用）都繼承 `LLMAdapter`，留給 Task 5 處理。
  - 新增：
    - 說的話在任何工具執行之前就到 sink。
    - 一個回覆兩個工具呼叫：照順序執行，產生兩則 `tool` 訊息，`tool_call_id` 對得上。
    - 第二輪的 messages 開頭和第一輪一個 byte 都不差，而且 assistant 訊息裡有 `reasoning_content`。
    - 呼叫沒交給她的工具：被拒、結果送回去、trace 裡看得到。
    - `Shell` 的回執送回去，回合不等 job。
    - 第一輪只呼叫 `MoodTool`：會跑第二輪，第二輪說的話有送到 sink（變啞的情況）。
    - `length`：什麼都不執行。
    - 回合最多 8 輪。
    - 連續兩個回合的 system 訊息完全相同。
    - think_chars 來自 `ThinkDelta`。
  - 改寫（它們釘住舊規則）：
    - `test_mind_loop.py`、`test_mind_loop_evolution_wiring.py`、`test_mind_loop_self_profile.py`、`test_mind_loop_trace.py` 裡斷言 `<tool_response>` 或 `IN_TURN_REFEED_TOOLS` 的測試，改成 `tool` 訊息和新規則。
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
  - 思考額度用同一個設定值；各 agent 現有的 `max_tokens`（workflow 4096、consolidation 和 evolution 2048）意思變成回答的額度。
  - workflow 的 cascade_log callback 拿掉 Task 4 的 `</think>` 暫時切法，直接傳 `reasoning`、`speech`。
  - kernel 把 `self.model` 傳給 WorkflowRunner、ConsolidationTrigger、EvolutionTrigger。
- 測試：
  - `test_tool_loop.py`（7）、`test_workflow.py`（23）、`test_consolidation.py`（17）、`test_evolution_keeper.py`、`test_evolution_trigger.py` 改用 `FakeModel`；斷言 `<tool_response>` 的地方改成 `tool` 訊息。
  - `_dispatcher_helpers.py`：`_HangAdapter` 改成會卡住的 `FakeModel`，`_FakeAdapter` 刪掉。之後 `tests/` 裡就沒有東西 import `LLMAdapter`，Task 7 才刪得掉。
  - 新增：`purpose` 有寫進 `LLMCallRecord`；16 輪上限；一個回覆裡同時有 `Report` 和別的工具時，那一輪做完就結束。

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

### Task 8 — 文件

- 檔案：`CLAUDE.md`。
- 改法：第 15、63、220-225 行改成：模型介面是 `/v1/chat/completions` 加原生工具，沒有 grammar；server 是 Strata（URL、1 個 slot、只收文字）；思考上限用 `reasoning_budget_tokens`；prompt 前綴要固定，cache 才會命中。第 5、153、258 行還寫著 llama.cpp / `model_id`，一起改。
- 測試：無，只改文件。

### Task 9 — 對真的 server 跑一次，記下第一批數字

- 準備：
  - `config.toml` 指向 Strata，語音和 Discord 關掉，`[data] root` 指到一個新的空目錄，不碰真的記憶。`[trace] root` 不跟著 `[data] root`（預設 `data/traces`），也指到同一個新目錄。
  - 先看 `/metrics` 的 `live`：有別人在用的話記下來，因為那會影響數字。
- 開機：
  - `uv run dollos --config config.toml`，檢查通過。
  - 把 `model` 改錯再開一次：要拒絕開機，錯誤訊息寫出設定的 model 和 server 實際的 model。
- 用 `experiments/ws_client.py` 跑 5 個回合：
  1. 「Hi Gura, what are you up to?」（冷的）
  2. 緊接著：「Tell me one thing you like about the ocean.」（system 和工具應該命中）
  3. 「Remember that my dentist appointment is Friday at 3.」→ 要呼叫 `NoteMemory`、有說話、資料目錄裡有那則筆記。
  4. 「What do I have on Friday?」→ 呼叫 `Recall`，第二輪的回答用到結果。
  5. 「Remind me Thursday evening about the dentist.」→ 呼叫 `WriteSchedule`，巢狀的 `entries` 正確，排程有存起來。
- 過關：
  - 5 個回合都沒有錯誤，回覆是英文。
  - 唸出來的文字裡沒有 `<tool_call>`、`SEEN:` 這類東西。
  - 每個多輪回合的第二輪 `cache_n` ≥ 第一輪的 `prompt_tokens`（代表訊息有原封不動送回去）。第二輪 `think_capped` 的話 `cache_n` 是 None，改看它的 TTFT 有沒有遠小於第一輪。
- 記下數字（從 `llm_calls` 和 `turn_latency` 的 JSONL）：
  - 每個回合：`prompt_tokens`、`cache_n`、`prompt_n`、`prompt_ms`、TTFT、`first_speak_ms`、think_chars、解碼 tok/s、輪數、`finish_reason`、`think_capped`。
  - 彙總：冷的 TTFT 對命中的 TTFT、cache 命中率（`cache_n / prompt_tokens`，只算 `think_capped` 是 False 的呼叫）、思考上限觸發的比例、解碼速度。
- 寫成一頁 `docs/research/2026-10-18-strata-first-numbers.md`，給你看。
- 之後：Gura 英文 plan 卡住的 Task 6 smoke（`scripts/persona_stability_smoke.py`）也能跑了。

---

## 已知代價（先量，這個 plan 不修）

- **第一次開口會很慢**。我從探測估計的數字如下，Task 9 會量實際的值：
  - 每回合的 user 區塊（估 2–3k token）都要重算，約 8–12 秒。
  - 說話之前要先思考：33 tok/s 下，300 token 的思考約 9 秒。
  - 整個冷掉的回合（system 約 3.9k、工具約 4k、user 約 2–3k token）要 30–40 秒。
  - 修法是 harness S5 的 ContextBuilder（只往後加、不重新渲染的對話紀錄），加上她自己選思考額度。
- **Cache 會被擠掉**。每一種情況，下一個主人回合都要整個重算：
  - 回合種類換了，工具集合就不同：日記、反思、agenda、pulse、外部、safe mode；就連一般的內部回合和主人回合也差一個 `LearnName`。
  - 背景 agent 跑過：workflow、consolidation、evolution。
  - 這台 server 的其他使用者。

  這個 plan 只在每次呼叫記下 `purpose` 和 `cache_n`，讓這些情況看得到。要不要不讓背景 agent 插進主人的對話，是 mind-loop 的排程工作。
- **有工具的回合至少兩輪**。第二輪是命中的，只要算新加的工具結果：結果很短（像 `"ok"`）時 TTFT 約 0.3 秒；`Recall` 這種幾百 token 的結果要好幾秒（實測 372 token 3.6 秒）。之後還要再思考一次，也多佔用 slot。
- **背景 agent 在 1 個 slot 上排隊**。workflow 的 N 個 worker 實際上一個一個跑；consolidation 120 秒、evolution 240 秒、workflow 每個 agent 300 秒的上限沒改，在這台 server 上容易逾時。主人的回合也要排在已經在等的呼叫後面（consolidation 和 evolution 會被主人輸入取消，workflow 不會）。Task 9 不跑這些，之後看 `llm_calls` 的 `purpose` 再說。
- **有數字之後，一次問你一個問題**：
  1. 她自己的回合要不要改成固定一份工具清單，執行時再擋？這要修 harness §6.4。
  2. 要不要請 server 的主人打開 `--conversation-cache-mib` 或 `effort_position: end`？

---

## Spec 修正（列出來，跟這個 plan 一起審；spec 檔案之後另外改）

- **harness §9 S4**：「先包住現有的 `/completion` + grammar 路徑」在 Strata 上不能跑。S4 改成直接做 chat 的 ModelAdapter，並且跟 S6 的 chat_tools 部分一起提前，排在 S2、S3、S5 前面。§4 的工具合併仍留在之後。
- **harness §9 S6、S8、S9**：`[harness].model_api` 的開關和兩種 `model_api` 的 A/B，在這台 server 上沒有意義，因為只有一條路徑跑得起來。S9「移除其中一條路徑」由這個 plan 的 Task 7 做掉。
- **harness §3.1**：「llama.cpp `--jinja`」改成「任何 OpenAI 相容的 server」，現在用的是 Strata。
- **harness §3.3**：選做法 1（`reasoning_budget_tokens`，已實測）；做法 2 不做。
- **mind-loop §5**：「跟回合共用 transport 的 `Semaphore(2)`」改成 `max_concurrency = 1`（server 只有 1 個 slot）。
- **think A/B spec**：Arm A（grammar + 格式化 think）在這台 server 上不能跑。redesign §15 Q4（B 組要不要跑）也就沒有意義了。
- **redesign R1a**：「在現行 grammar 路徑上接 Qwen3.8」由這個 plan 取代。**R1b**：「量 think A/B Arm A 基準」改成 Task 9 在 chat 路徑上量的數字。

---

## 驗收

- Task 0 記錄的基準扣掉 Task 7 列出的 112 個舊測試，加上新測試，全部通過。除了上面列出的，沒有刪任何測試。
- `src/` 裡沒有 `/completion`、`grammar`、`ToolStreamParser`。
- daemon 對 Strata 開得起來；model 設錯時拒絕開機，錯誤訊息清楚。
- Task 9 的 5 個回合過關，數字已經寫好並給你看。

## 不在範圍內

- 工具合併成約 15 個（harness §4）、TurnPolicy（S3）、ContextBuilder/ToolRegistry/ToolExecutor 抽出和單一 Turn Runner（S5）、MCP client（S7）。
- 她自己選 low/mid/high 的思考額度；固定工具清單；背景 LLM 呼叫的排程；只往後加的對話紀錄。
- 圖片（這台 server 不收）、Anthropic adapter、assistant prefill、server 端的設定。
- 打斷的實機測試（`ws_client.py` 不會送打斷；行為由 Task 1 的單元測試保護）。
