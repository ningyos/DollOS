# Doll Harness：把 MindLoop 拆成設計過的 agent harness，動作改成程式碼

**狀態**：DRAFT（待使用者審）
**日期**：2026-10-17
**關聯**：`2026-10-17-think-ab-qwen38-design.md`（think A/B）、`2026-07-07-latency-reflex-think-design.md`、`2026-05-01-dollos-pivot-to-computer-design.md` §5（Core Loop）

---

## §0 為什麼要做

`MindLoop` 實際上就是 Doll 的 harness，但它是一路長出來的，沒有設計過：

- `mind_loop.py` 有 1,837 行，`_run_one_turn` 一個函式就 611 行。
- 每種回合用旗標區分（`_is_reflection` / `_is_diary` / `_is_agenda` / `_is_pulse`，被引用 43 次），工具範圍分散在 7 份清單裡（`MAIN_TOOLS`、`AGENDA_TOOLS`、`DIARY_TOOLS`……）。
- 有兩套 cascade 迴圈（即時回合一套，workflow / 演化一套）。
- 模型介面寫死在 llama.cpp + Qwen ChatML + GBNF 上。
- 25 個 pydantic 工具，每個都要寫 JSON schema、grammar 規則和說明文字，模型一次只能呼叫一個。

模型換成 Qwen3.8 之後（寫程式能力強很多），使用者的方向是：**少而通用的工具、用程式碼當動作、把環境抽象成 Python REPL，也可以考慮 WASM**。

---

## §1 一句話設計

> **Doll 的每個回合：自由思考 → 開口說話（純文字，立刻串流到 TTS）→ 需要做事時寫一段 Python → harness 在沙箱裡執行，把結果當成觀察回饋給她 → 直到她不再寫程式為止。**

她能做的事，全部都是沙箱裡的幾個 Python 物件（`memory`、`me`、`world`……）。**某種回合不該做的事，那個物件根本不存在於她的環境裡。**

---

## §2 Harness 的五個元件

```
                 Perception queue（既有，不變）
                          │
                          ▼
┌────────────────────── Turn Runner（迴圈）──────────────────────┐
│ 1. TurnPolicy.for(perceptions)      決定這回合的規則              │
│ 2. ContextBuilder.build(policy)     組出模型要看的內容            │
│ 3. ModelAdapter.generate(...)       串流：think / 說話 / 程式碼   │
│ 4. Interpreter.run(code, caps)      在沙箱執行，產生觀察          │
│ 5. 觀察送回 3，直到沒有程式碼、到達上限，或被打斷               │
└──────────────────────────────────────────────────────────────────┘
        │說話 → sink / TTS          │副作用 → 只能透過 Capabilities
```

| 元件 | 職責 | 取代現在的 |
|---|---|---|
| **TurnPolicy** | 每種回合一份宣告式設定：綁定哪些 capability、能不能說話、think 額度、最多幾輪 | `_is_xxx` 旗標、7 份工具清單、`_active_tool_registry` |
| **ContextBuilder** | 組 prompt：身份、記憶提示、最近的對話與觀察、身體與 vital 狀態 | `render_mind` 與散落在 MindLoop 的組裝邏輯 |
| **ModelAdapter** | 把模型輸出統一轉成事件：`ThinkDelta`、`SpeechDelta`、`CodeAction`、`End`；在這層落實 think 上限 | `ComposedLLMAdapter` + 樣板 + grammar + `ToolStreamParser` |
| **Interpreter** | 執行程式碼，回傳 stdout、回傳值、錯誤 | `dispatch_one` + 各工具的 `run()` |
| **Capabilities** | 沙箱裡能用的 Python 物件，所有副作用的唯一出口；政策（origin_tier、回合種類）在這裡落實 | 25 個 pydantic 工具 |

**Turn Runner** 只有一套。主回合、日記、agenda、workflow 的 worker、演化，全部是同一個迴圈搭配不同的 TurnPolicy。

---

## §3 動作 = 程式碼

### §3.1 輸出長什麼樣

````
<think>她說被裁員了，先陪她，順便記下來</think>
蛤……怎麼會這樣，你還好嗎？
```python
me.mood("worried", reason="使用者今天被裁員")
memory.note("使用者 2026-10-17 被裁員", tags=["life"])
```
````

- **說話是 code block 以外的純文字**，跟現在一樣邊產生邊送 TTS。不設 `say()` 函式，因為程式碼要等整個 block 完成才會執行，放在裡面會拖慢開口時間。
- 一個 code block 可以一次做好幾件事，也可以用迴圈、條件、字串處理把動作組合起來。現在的 JSON 工具一次只能呼叫一個。
- block 結束時 harness 執行它，把輸出當成觀察，接著開下一輪；她寫完程式後也可以繼續說話。

### §3.2 跟模型的介面（ModelAdapter 處理）

| 後端 | code action 的形式 |
|---|---|
| llama.cpp 原始 `/completion`（現在的路徑） | 輸出裡的 ```` ```python ```` fenced block，串流時偵測 |
| OpenAI 相容 chat API / Anthropic | 一個原生工具 `python(code: str)` |

harness 核心只看得到 `CodeAction(code)`，不管它是從哪種格式來的。**BYO LLM 因此變得可行**：不用為每個工具維護 grammar 或 schema，只要模型會寫 Python。

grammar 要不要保留、保留多少，由 think A/B 的結果決定，屬於 ModelAdapter 內部的事。

### §3.3 Fire-and-forget 不變

外部動作（shell、monitor、workflow）的 capability **立刻回傳一個 job 物件**，結果之後以 `ToolResultArrived` perception 回來，跟現在一樣。沙箱裡沒有 `await`，也沒有「等結果」的函式，「Doll 不能 await 自己的嘴」這個原則不變。

---

## §4 Capabilities：沙箱裡的世界

現有 25 個工具收斂成幾個物件。下表是初稿，實際名稱和簽名在實作 plan 裡定：

| 物件 | 方法（舉例） | 取代的工具 |
|---|---|---|
| （純文字） | 說話 | Say（本來就是純文字） |
| `memory` | `note()`、`search()`、`read_day()` | NoteMemory、Recall |
| `diary` | `write()` | WriteDiary |
| `me` | `mood()`、`focus()`、`pin()`、`revise()`、`lesson()` | MoodTool、SetFocus、PinSelf、SelfRevision、NoteToolLesson |
| `people` | `learn_name()` | LearnName |
| `agenda` | `open_loop()`、`close_loop()`、`pursue()`、`advance()` | OpenLoop、CloseLoop、PursueGoal、AdvanceGoal |
| `schedule` | `add()` | WriteSchedule |
| `world` | `shell()`、`monitor()`、`stop_monitor()`、`workflow()` | Shell、SpawnMonitor、RemoveMonitor、SpawnWorkflow |
| `skills` | `read(name)` | InvokeSkill |
| `scratch` | 一個跨回合保存的 dict | Scratchpad |
| `report()` | 只綁定在 worker 回合 | Report |
| 結果物件 | `.text[a:b]`、`.grep(pattern)` | ReadToolOutput、GrepToolOutput（變成一般的 Python 操作） |

- 每個方法的參數仍用 pydantic 驗證。驗證失敗變成錯誤訊息回給她，跟現在工具失敗回饋的方式一樣。
- **政策由綁定落實，不靠過濾**：`external_public` 的回合（陌生人、MCP 對端）的環境裡根本沒有 `world`；日記回合只有 `diary` 和 `memory`。不存在的名字，呼叫了就是 `NameError`，沒有繞過的可能。
- 每個 capability 呼叫都寫進 action log（日記需要它）和 trace。

---

## §5 沙箱（Interpreter）

### §5.1 兩種程式碼，兩種風險

1. **心智操作**（記憶、mood、agenda）：要碰 daemon 的狀態，本身是安全的。
2. **世界操作**（shell、檔案、網路）：有風險，但現在的 `Shell` 本來就允許，政策沿用。

所以沙箱本身**不需要**能碰檔案或網路；它只負責「組合」，所有副作用都經過 host 提供的 capability。這正好是 deny-by-default 型沙箱的設計。

### §5.2 選項

| 方案 | 啟動 | 隔離方式 | Python 完整度 | 狀態保存 | 評語 |
|---|---|---|---|---|---|
| **Pydantic Monty**（Rust 寫的 Python 子集直譯器，跑在 process 內） | 微秒級（官方數據） | 預設全禁，只能呼叫 host 給的函式 | 子集：沒有 class、標準庫很少、不能 import 第三方套件 | 可以把整個直譯器狀態序列化 | 跟 §5.1 的需求最吻合；但還是 0.0.x 的實驗版 |
| **CPython 編成 WASM**（WASI + wasmtime / Wasmer） | 約數十 ms | capability-based | 幾乎完整 | 要自己處理 | 完整但比較重 |
| **Pyodide** | 秒級（官方比較約 2.8 s） | 瀏覽器式 WASM 沙箱，不是為伺服器端隔離設計 | 完整 | 長駐的話可以 | 啟動慢、偏瀏覽器用途 |
| **CPython 子行程 REPL** | 快 | 只有 process 隔離，要再包 container 才算安全 | 完整 | 長駐 | 隔離最弱 |

### §5.3 建議

- **v1 用 Monty**，因為「只能呼叫 host 給的 capability」正是我們要的安全邊界，而且幾乎沒有啟動成本。
- **Interpreter 是一個介面**，Monty 只是第一個實作。Monty 還是實驗版，萬一卡住，可以換成 WASI CPython，Turn Runner 和 capabilities 都不用動。
- Monty 不支援 class、標準庫也少，這對「組合幾個 capability」的程式碼影響不大。需要完整 Python 的重工作（資料處理之類），之後再加一個 `world.python_job()`，用 fire-and-forget 的方式丟到 WASM 或 container 裡跑。不在 v1。
- **狀態**：每回合開一個新的直譯器，變數只在同一回合的多輪之間保留。跨回合要記住的東西只有兩條路：`scratch`（小型 dict，隨 MindState 保存）或 `memory`。這樣才不會在記憶 SoT 之外藏一份看不見的狀態。

---

## §6 遷移計劃

一次只換一塊，每一步測試都要全綠。

| 步驟 | 內容 | 行為改變 |
|---|---|---|
| **H1** | 抽出 `TurnPolicy`，取代旗標和 7 份工具清單 | 無（純重構） |
| **H2** | 抽出 `ModelAdapter`，把樣板、grammar、parser 收進去，輸出統一的事件 | 無（純重構） |
| **H3** | 抽出 `ContextBuilder` 和單一的 Turn Runner；workflow / 演化改用同一個迴圈 | 無（純重構，刪掉重複的 cascade） |
| **H4** | 加入 Interpreter（Monty）和 capabilities；用設定 `[harness].action_mode = "json_tools" \| "code"` 明確切換 | code 模式是新的 |
| **H5** | 在 think A/B 的情境集上比較 `json_tools` 和 `code`（同樣的評分方式：機械檢查 + Claude 成對盲評 + 使用者抽查） | — |
| **H6** | 依結果移除其中一條路徑，`action_mode` 開關一起拿掉 | — |

`action_mode` 是**實驗期間的明確開關**，不是 fallback：不會在執行中自動切換，H6 之後就刪掉。

**跟 think A/B 的順序**：think A/B 可以先在現有程式上跑 Arm A，取得 Qwen3.8 的基準。H1–H3 是純重構，可以接在後面；H4 以後的比較直接沿用 A/B 的情境集和評分工具。

---

## §7 不變的部分

- perception 佇列、WAL、單一消費者的 mind loop 模型
- 記憶（markdown 為準 + FTS5 索引）
- Self-First：身份描述而不是行為命令
- fire-and-forget 外部動作、打斷機制
- origin_tier 的記憶範圍規則（改由 capability 綁定落實）

---

## §8 風險

1. **Monty 還是實驗版**（0.0.x），API 會變。對策：Interpreter 介面加上版本鎖定；最壞情況換成 WASI CPython。
2. **模型寫出 Monty 不支援的語法**（例如 class）。錯誤會以觀察回給她，她可以改寫；如果太常發生，就在 prompt 裡說明子集範圍，或換 Interpreter。
3. **說話和程式碼交錯時的延遲**：她可能先寫程式、後說話，開口時間就會變晚。由 H5 的 `first_speak_ms` 量測；需要的話，在 prompt 裡把「先說話」寫進身份描述，而不是用 grammar 強制。
4. **重構期間的回歸**：H1–H3 都是純重構，靠現有約 1,670 個測試把關。

---

## §9 待決問題

1. **Q1**：capability 的命名要用英文（`memory.note`）還是中文？模型寫英文的程式碼通常比較穩，建議用英文。
2. **Q2**：`scratch` 要不要設大小上限？建議 8 KB，超過時回報錯誤，不自動截斷。
3. **Q3**：H1–H3 這段純重構，要先做，還是等 think A/B 的 Arm A 基準量完再開始？
