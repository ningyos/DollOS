# Spec A — 工具系統打磨（Tool System Polish）

- **Date**: 2026-06-27
- **Status**: Design (approved to implement)
- **Scope**: 工具系統的機械性打磨——讓 Doll 用得襯手、砍掉「機械型」fail-tooling。
- **Out of scope**: 跨-turn 工具學習迴路（工具記憶 / 慣性 / per-tool 成功率）= **Spec B**，另開。

---

## 1. 動機（審視結論）

2026-06-27 對工具系統（`tools.py` / `mind/mind_loop.py` / `mind/mind_state.py` /
`mind/mind_prompt.py` / `llm/templates.py`）做了完整審視。機制本身扎實
（Pydantic 工具 + GBNF + streaming parse + in-turn cascade + safe-mode 護欄），
但有四類機械性問題會讓 Doll「用不順手」並製造 fail-tooling：

1. **Grammar 只 emit `required` 欄位** → 任何有 default 的 optional 參數在 grammar
   下「物理上無法被產生」。實際被鎖死的能力：
   - `Shell.timeout_s` / `SpawnSubagent.timeout_s` / `SpawnMonitor.rate_limit_s`
     永遠用 default（無法為長命令調大 timeout）；
   - `SpawnMonitor.match_regex` 永遠 `None`（docstring 裡兩個帶 regex 的範例
     根本產生不出來，每行都 fire）；
   - `Recall.since` / `Recall.until` 永遠無法做日期過濾（描述卻細講用法）；
   - `MoodTool.reason` 永遠空白。
   這是「描述承諾的能力 vs grammar 實際能做的」沉默落差，違反專案
   「state boundaries clearly / no fallback」原則。

2. **整數 grammar 與 schema 語意打架**：`integer ::= "0" | [1-9][0-9]*` 只收非負，
   但 `ReadToolOutput.offset` 描述說「negative counts from end」→ 該能力不可達；
   且 grammar 放行任意非負整數（0、99999），pydantic 的 `ge/le` 事後才擋 →
   製造 validation fail。

3. **「單一工具 build 失敗 → 全部 unconstrained」的沉默懸崖**：grammar 一次性從
   整個 registry build；任一工具的型別未支援（float / dict / …）會讓
   `build_voice_first_grammar` raise，被 `MindLoop.__init__` 的
   `except Exception` 吞掉 → `self._grammar = None` → 整個 loop 無 grammar 跑
   （工具名可亂編、JSON 可亂吐）。fail-tooling 暴增且難察覺。

4. **架構債**：兩套單一工具 dispatch 並存——
   - live loop：`mind_loop._dispatch_tool`（吃 `MindCtx`）；
   - subagent：`subagent.py` → `cascade/tool_loop.run_tool_cascade` →
     `dispatch_tool_call`（吃 `ToolCtx`，B4 grammar）。
   兩者靠「手動 mirror」保持一致（`_dispatch_tool` docstring 寫
   "Mirrors cascade.tool_loop.dispatch_tool_call semantics"）→ drift 風險。
   `dispatcher.py` 早已不存在；`ToolCtx` 僅剩上述 subagent 路徑在用，其
   "kept until Task 8 deletes dispatcher" 註解已過時。

另外：工具的失敗訊息對 LLM 不友善（validation 失敗直接吐 pydantic 原始錯誤牆；
unknown tool 不給有效名單），以及狀態管理工具疊床架屋（scratchpad 4 件 +
SetFocus + OpenLoop/CloseLoop = 7 件，其中 `EditScratchpad` 的 exact-unique
子字串匹配是最大 runtime fail 源）。

---

## 2. 目標 / 非目標

**目標**
- Doll 能設定目前被 grammar 鎖死的 optional 參數（timeout / regex / 日期過濾 / mood 理由）。
- 整數 grammar 與 schema 語意對齊（負數可達；shape 由 grammar、semantics 由 pydantic）。
- 移除「單一工具 build 失敗→全部 unconstrained」的沉默懸崖（改為啟動時大聲 raise）。
- 工具失敗時 Doll 拿到精簡、可行動的錯誤訊息（欄位名 + 期望 + 你給的值）。
- 工具組精簡：scratchpad 4→1、砍 EditScratchpad。
- 單一工具 dispatch 與錯誤格式只有一份定義，live loop 與 subagent 共用。

**非目標（YAGNI / 留給 Spec B）**
- 跨-turn 工具記憶、per-tool 成功率統計、慣性/習慣表面。
- 把 `arguments` 改成 free-form JSON（會回退 fail-tooling，明確拒絕）。
- cascade 編排的統一（voice-first vs B4、re-feed vs Report early-exit 各自保留）。

---

## 3. 設計

### § 3.1　Grammar：optional 參數可達

`llm/templates.py::_build_tool_call_rule` 擴充，分兩段組 arguments body：

1. **required 段**：照舊，依 `schema["required"]` 順序全部 emit（`str` / `integer` /
   string-enum / `$ref`-array）。
2. **optional 後綴段**：對 `properties` 中**不在 `required`** 的欄位，依 schema
   宣告順序，各生成一個「可有可無、自帶前導逗號」的片段：

   ```
   ( "," "\"<name>\": " <type> )?
   ```

   因為每個 optional 片段都自帶前導逗號，且 required 段已先 emit 至少一個欄位，
   產出的 JSON 永遠合法。

**型別支援（optional）**：`string`、`integer`。對 `X | None`（如 `match_regex: str | None`、
`since/until: datetime | None`）：json schema 會以 `anyOf: [{type:...}, {type:"null"}]`
表示。解析時抽出非-`null` 的型別當作該 optional 欄位型別。**不需要在 grammar emit
`null`**——Doll 要設值就 emit 該欄位（字串/整數），不要就省略，省略即取 pydantic default。
`datetime` 在 schema 是 `string`（format date-time），對 grammar 而言就是 `str`，
pydantic 之後負責解析 ISO。

**邊界**：目前每個工具都有 ≥1 required 欄位，故「0 required + 有 optional」的前導逗號
邊界不會發生。若未來出現此情形，`_build_tool_call_rule` 須**明確 raise
`NotImplementedError`**（附清楚訊息），不得靜默產生非法 JSON。

`build_qwen3_think_tool_grammar`（B4，subagent 用）與 `build_voice_first_grammar`
（voice，live 用）都呼叫同一個 `_build_tool_call_rule`，故兩者一併受惠。

→ **解鎖能力**：`Shell.timeout_s`、`SpawnSubagent.timeout_s`、
`SpawnMonitor.match_regex` / `rate_limit_s`、`Recall.since` / `until`、
`MoodTool.reason`。

### § 3.2　Grammar：整數規則對齊

`_JSON_STR_RULES` 的 integer 規則改為支援帶負號的整數：

```
integer ::= "-"? ( "0" | [1-9] [0-9]* )
```

- 解鎖 `ReadToolOutput.offset` 的「負數從尾端數」。
- grammar 只負責 **shape**（是一個整數），**semantics**（範圍 `ge/le`）仍由 pydantic
  守門；超界 → 走 § 3.4 的友善錯誤回 Doll。這與 ref
  「Constrained Decoding = Shape not Semantics」一致。

### § 3.3　移除 grammar build 的沉默懸崖（no-fallback）

`MindLoop.__init__` 目前：

```python
try:
    self._grammar = build_voice_first_grammar(list(self._tool_registry.values()))
except Exception:
    logger.exception("failed to build voice_first grammar; running unconstrained")
    self._grammar = None
```

改為**不吞例外**：grammar build 失敗 = 工具集設定錯誤，屬於必須在啟動時暴露的
組態問題。讓例外往上拋（daemon 拒絕用半殘/無約束的工具集起來），符合
no-fallback + surface-not-blank。`_build_tool_call_rule` 對未支援型別已 raise
`NotImplementedError`——這個 raise 從此會浮上來，而非被靜默轉成
unconstrained decode。

（safe-mode 的 `_active_grammar()` 已是「reduced 工具集必須 build 成功、失敗則 raise」
的正確語意，無需更動。）

### § 3.4　錯誤訊息友善化

單一工具 dispatch（見 § 3.6 合一後）對失敗回傳**精簡、可行動**的 `ToolResult.detail`：

- **args 驗證失敗**：把 `ValidationError` 攤平成每個錯誤欄位一行：
  `<欄位名>: <期望> (你給了 <值>)`。例：
  `ReadToolOutput 參數錯誤：limit 需 1–500 整數（你給了 0）`。
  不再把 `str(e)` 的 pydantic 原始錯誤牆塞進 detail。
- **unknown tool**：附目前有效工具名清單，
  例：`未知工具 'Foo'。可用工具：NoteMemory, Shell, Recall, …`（對齊 `InvokeSkill`
  既有的 graceful 風格）。
- **runtime error**：保留 `runtime error: <e>`，但訊息以一句話為主、不附 traceback。

格式統一由 § 3.6 的共用 dispatch 提供 → live loop 與 subagent 拿到一致的友善錯誤。
這些訊息透過 in-turn `<tool_response>` re-feed 回 Doll（外部 grounding，現行行為不變）。

### § 3.5　工具去重 / 精簡

- **scratchpad 4 件 → 1 件**：
  ```python
  class Scratchpad(BaseModel):
      op: Literal["set", "append", "clear"]
      content: str  # required（保 grammar 強制）；op="clear" 時忽略 content
  ```
  - `set`：覆寫；`append`：附加一行；`clear`：清空（忽略 content）。
  - `content` 設為 required 而非 optional——讓 grammar 強制 set/append 一定帶
    content（避免「op=set 但沒 content」的 validation fail）。`clear` 帶一個被忽略的
    content 是可接受的小代價。
  - 內部沿用既有 `scratchpad_helpers`（write / append / clear）。
- **砍 `EditScratchpad`**：exact-unique 子字串匹配是最大 runtime fail 源；set + append
  已涵蓋實務需求。
- **保留** `SetFocus` / `OpenLoop` / `CloseLoop`（語意各異、有結構價值）。
- 同步更新 `MAIN_TOOLS` / `SUB_TOOLS`（移除 4 個舊 scratchpad 類、加入 `Scratchpad`）。
- 清掉過時的「REQUIRED — do not omit」哀求式描述（`ReadToolOutput`）——offset/limit
  是 integer+required，grammar 已強制，哀求語是 `grammar=None` 時代遺跡。

### § 3.6　架構債：dispatch 合一

把「單一工具 dispatch」抽成**一份共用函式**，吃 `MindCtx`：

```
validate args → run(ctx) → 分類成 ToolResult / None（含 § 3.4 友善錯誤）
```

- `mind_loop._dispatch_tool` 改為呼叫此共用函式（薄包裝，套用 `_active_tool_registry()`）。
- `cascade/tool_loop.run_tool_cascade` 內的 `dispatch_tool_call` 也改呼叫同一份。
- **subagent 遷到 `MindCtx`**：subagent cascade 目前建 `ToolCtx`，改為建 `MindCtx`
  （subagent 沒有對外 sink → 用 null/no-op sink resolver；mind_state 用 subagent 自己的
  輕量 state 或一個拋棄式 `MindState()`）。遷移完成後**刪除 `ToolCtx`**。
- cascade **編排**不動：subagent 仍用 B4 grammar + `run_tool_cascade` + Report
  early-exit；live loop 仍用 voice-first + 多-pass re-feed。只統一「單一工具 dispatch +
  錯誤格式」這一層。

> **若 subagent→MindCtx 遷移在實作時證實過於侵入**（MindCtx 依賴的欄位 subagent 難以
> 提供）：退而求其次，保留 `ToolCtx`，但讓兩條路徑都 route 過同一個 dispatch helper
> （helper 以一個窄介面 protocol 吃 ctx），仍消除手動 mirror。此 fallback 僅限架構債這
> 一桶，且須在 plan 階段明確記錄選擇了哪條。

---

## 4. 受影響檔案（預估）

- `src/dollos/llm/templates.py` — `_build_tool_call_rule`（optional 後綴、anyOf 抽型別、
  0-required raise）、`_JSON_STR_RULES`（signed integer）。
- `src/dollos/mind/mind_loop.py` — `__init__`（移除 grammar build 吞例外）、
  `_dispatch_tool`（改呼叫共用 dispatch）。
- `src/dollos/tools.py` — 新增 `Scratchpad`、移除 `WriteScratchpad`/`AppendScratchpad`/
  `EditScratchpad`/`ClearScratchpad`，更新 `MAIN_TOOLS`/`SUB_TOOLS`、清過時描述。
- `src/dollos/cascade/tool_loop.py` — `dispatch_tool_call` 改呼叫共用 dispatch；
  友善錯誤格式。
- `src/dollos/subagent.py` — 遷 `ToolCtx` → `MindCtx`（或 § 3.6 fallback）。
- 共用 dispatch 的落點：新增 `src/dollos/cascade/dispatch.py`（或併入 `tool_loop.py`），
  在 plan 階段決定。
- 移除 `ToolCtx`（`tools.py`）。

---

## 5. 錯誤處理

- **no-fallback 一致**：grammar build 失敗 = 啟動 raise（§ 3.3）；safe-mode grammar 維持
  build 失敗即 raise。
- 工具 runtime error / validation fail 仍走 in-turn re-feed（外部 grounding），訊息走
  § 3.4 友善格式；同 tool 連 3 次失敗 / K 次連續失敗 → 既有 safe-mode 不變。

---

## 6. 測試

- **grammar / templates**
  - optional 欄位：present（帶值）與 absent（省略）都能被 grammar 接受、被
    `ToolStreamParser` 解析、被 pydantic 還原。
  - `X | None` 的 anyOf 型別抽取正確（match_regex / since / until）。
  - signed integer：負數、0、正數都接受；`-` 後不接合法數字則拒絕。
  - 「0 required + 有 optional」→ `_build_tool_call_rule` raise（不得產非法 JSON）。
  - 一個帶未支援型別的假工具讓 `build_voice_first_grammar` raise → `MindLoop.__init__`
    **不**吞、往上拋（不再產生 `grammar=None`）。
- **友善錯誤**
  - args 驗證失敗訊息含欄位名 + 期望 + 給定值，且**不含** pydantic 原始 traceback/牆。
  - unknown tool 訊息含有效工具名清單。
- **工具精簡**
  - `Scratchpad` set/append/clear 三種 op 行為正確；clear 忽略 content。
  - registry 不再含 `WriteScratchpad`/`AppendScratchpad`/`EditScratchpad`/`ClearScratchpad`；
    含 `Scratchpad`。
- **dispatch 合一**
  - live loop 與 subagent 兩條路徑共用同一 dispatch、行為一致；既有 `test_tools.py` /
    `test_subagent.py` 綠。
- 全套既有測試（626+）綠；上述新增測試綠。

---

## 7. 風險

- **subagent→MindCtx 遷移**是 § 3.6 最大改動面；plan 階段需先評估 MindCtx 對 subagent 的
  依賴，必要時走 § 3.6 fallback。
- **optional 後綴的 grammar 正確性**：GBNF 逗號處理易錯，需以多組 present/absent 組合的
  解析測試把關。
- **Scratchpad 行為改變**：Doll 既有「用 Edit 改 scratchpad」的習慣會失效；以 append/set
  覆蓋。屬可接受的行為遷移（且 Spec B 的工具記憶會幫她適應）。

---

## 8. 與 Spec B 的關係

Spec A 刻意**只做機械層**。Spec B（工具記憶 / 慣性 / per-tool 成功率 / 慣性表面）將建立在
A 打磨後的乾淨工具層 + 統一 dispatch 之上——統一 dispatch 是 B 注入「記錄每次工具
成功/失敗」掛鉤的單一落點。
