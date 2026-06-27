# Spec B — 工具記憶 / 慣性（Tool Memory & Habits）

- **Date**: 2026-06-27
- **Status**: Design (approved to implement)
- **Scope**: 跨-turn 工具學習迴路——讓 Doll 從工具成功/失敗中累積記憶，**減少重蹈覆轍的 fail-tooling** + 形成正向**慣性/習慣**。建在 Spec A 的 `dispatch_one` 咽喉點上。
- **Depends on**: Spec A（`dispatch_one` 單一 dispatch、友善錯誤、grammar optional 參數）已 merge 進 main。

---

## 1. 動機

2026-06-27 工具系統審視結論：機制扎實，但**完全沒有跨-turn 的工具學習迴路**。具體缺口：
- 工具失敗只透過 in-turn `<tool_response>` re-feed 出現，turn 結束即蒸發 → 明天同樣的錯照犯。
- `_record()` 只在成功路徑呼叫，**失敗根本不寫進持久狀態** → Doll 的自我史是成功偏誤的。
- 無 per-tool 成功/失敗統計、無工具慣性表面、無從成功模式長出的「playbook」。

對照已收的設計記憶 **ACE / ReasoningBank（[[ref_playbook-over-transcripts-ace-reasoningbank]]）**：自我改進 = append-only 的 strategy/pitfall delta（從成功 **AND** 失敗 distill），不是改寫式摘要（改寫會 context collapse）。ReasoningBank 的增益很大一部分來自**失敗衍生的 pitfall lessons**。這是 gradient-free，契合單一大模型、無 fine-tune 的 DollOS，且是 Self-First substrate。

Spec A 已把所有工具呼叫收斂到單一 `dispatch_one`——這是注入「記錄每次成功/失敗」掛鉤的天然落點。

---

## 2. 目標 / 非目標

**目標**
- 工具失敗持久化並回饋給 Doll，使她**避免重蹈**同一個工具錯誤（hot-path、精簡）。
- 從工具成功+失敗 distill 出 compact、append-only 的「工具課程」（playbook），在相關情境時 surface，形成慣性。
- 學習迴路 gradient-free、reflector 在 idle（不加 hot-path 延遲）。

**非目標（YAGNI / 未來）**
- usefulness-counter / 課程評分排序（ACE 進階）。
- 工具序列 n-gram 慣性（v1 用 per-tool lessons）。
- 跨角色私有 playbook（v1 用 shared namespace）。
- 改寫式 playbook 摘要（明確拒絕——append-only only，避免 context collapse）。
- 把記錄做成 fallback/降級邏輯（記錄是觀測性；失敗不得影響工具 dispatch 本身）。

---

## 3. 設計

兩層：Layer 1 機械型失敗記憶（always-on、hot-path、精簡）；Layer 2 playbook（idle reflector、append-only）。

### § 3.1　Layer 1 — 機械型失敗記憶

**記錄點**：`mind_loop._dispatch_tool`（**live-only** wrapper，在 `dispatch_one` 回傳後）。
**不在 `dispatch_one` 本體記錄**——保持它純淨，且 subagent 路徑（用 ephemeral sub-state）不污染 Doll 的記憶。

**新增資料結構**（`mind_state.py`）：
```python
@dataclass
class ToolFailure:
    t: float
    tool: str
    detail: str   # truncated friendly-error detail
```
MindState 新增兩個扁平欄位（與既有 deque[dataclass] / dict 模式一致）：
```python
tool_stats: dict[str, dict[str, int]] = field(default_factory=dict)
# e.g. {"Shell": {"ok": 3, "fail": 1}}
recent_tool_failures: deque[ToolFailure] = field(default_factory=lambda: deque(maxlen=10))
```

**記錄邏輯**（新模組 `dollos/mind/tool_memory.py`，`record_tool_outcome(mind_state, name, result)`）：
- `result is None`（side-effect，乾淨跑完）→ `stats[name].ok += 1`
- `result.success is True` → `stats[name].ok += 1`
- `result.success is False` → `stats[name].fail += 1` 且 append `ToolFailure(t=now, tool=name, detail=result.detail[:200])`
- 包在 try/except（log 後續跑）——記錄失敗不得讓 dispatch 失敗。

`_dispatch_tool` 在 `r = await dispatch_one(...)` 後呼叫 `record_tool_outcome(self._ctx.mind_state, name, r)`。

**Surface**：`mind_prompt` 新增 `[Tool notes]` block，**僅在有近期失敗時出現**：
- 時間窗：只取 `now - t < TOOL_NOTE_WINDOW_S`（預設 3600s=1h）的失敗。
- **cap 順序（spec-review）**：先依 tool 去重（保留每個 tool 最新一筆），再依最新失敗時間 desc 排序，取前 5；detail 截斷（≤100 chars）。
- 無符合者 → 整個 block 不 render（零 token）。

**計數校準（spec-review）**：`tool_stats` 記的是 dispatch 結果——fire-and-forget 工具（Shell / SpawnSubagent / SpawnMonitor）的 `run()` 回傳 dispatch ack 字串（success），故計為 `ok` 代表「dispatch 被接受」，**非命令本身成功**（命令結果之後以新 perception 回來）。文案/課程措辭需意識此點。
- 文案：
  ```
  [Tool notes] 最近工具失敗（避免重蹈同樣錯誤）：
  - ReadToolOutput: limit 需 1–500 整數（你給了 0）
  - Shell: timeout after 60s
  ```
- 渲染器 `render_tool_notes(recent_tool_failures, now)` 放 `tool_memory.py`，`mind_prompt` import。

### § 3.2　Layer 2 — Playbook（idle reflector，append-only）

**新工具 `NoteToolLesson`（reflection-only，不在常駐 hot-path 工具集）**：
```python
class NoteToolLesson(BaseModel):
    """Record a compact, reusable lesson about HOW to use your tools —
    distilled from what worked or what failed. Append-only; write a new
    lesson rather than rewriting an old one."""
    situation: str = Field(description="When this applies, one phrase.")
    lesson: str = Field(description="The reusable takeaway, one or two sentences.")
```
- **Gating（ACE-fidelity，spec-review #6）**：`NoteToolLesson` **不**進 `MAIN_TOOLS`。改放 `REFLECTION_TOOLS = MAIN_TOOLS + [NoteToolLesson]`，**只在 reflection turn** 進入 grammar/registry。理由：好課程需 grounding 於 ok/fail pattern，而那只有 reflection turn 的 `[Tool outcomes]` 提供；常駐 hot-path access 會誘發「剛失敗就衝動寫失敗課程」這種無 grounding 的低品質 entry（append-only 無法事後修正）。對齊 [[ref_intrinsic-reflection-is-net-negative-without-external-grounding]]。
- **實作**：沿用 Spec A 既有的 `_active_tool_registry()` / `_active_grammar()` 機制——`MindLoop` init 建 base grammar（MAIN_TOOLS）；reflection grammar（REFLECTION_TOOLS）**lazy 建一次並 cache**（比照 `_safe_grammar`）。per-turn 依 `is_reflection`（見 §3.2 reflector）選 registry+grammar。safe_mode 優先序高於 reflection（safe_mode 時不開 NoteToolLesson）。
- `run`：append 一條 markdown entry 到 `memory_root/shared/tool_playbook.md`，然後 `await ctx.memsearch.index_file(path)`。append-only。`_record(ctx, "NoteToolLesson", ...)`。回傳 `"lesson noted: {situation[:60]}"`；**不**列入 `IN_TURN_REFEED_TOOLS`（不需 re-feed 一個確認字串）。
- **entry 格式（timestamp-only heading，spec-review #3）**：`## {datetime.now():%Y-%m-%d %H:%M:%S}\n\n[situation] {situation}\n{lesson}\n`。**不可用 `build_heading`**——它嵌入 mood/tod/dow 的 `[k:v]` 軸標籤，`associative_search` 會以那些軸過濾，導致 tool lessons 洩漏進 `[Associative memories]`。timestamp-only heading 讓 `parse_heading` 得 `tags={}`，被所有軸排除。

**Reflector = 既有 `ReflectionMoment`**（不新增排程）：
- **Reflection 偵測（gate on drained batch，spec-review #5）**：在 `iterate()` 算 `is_reflection = any(p.kind == "ReflectionMoment" for p in perceptions)`（**當前 drain 的整批**，非 `recent_perceptions[-1]`——否則 `[ReflectionMoment, UserSpoke]` 這種批次會讓尾判為 false 而靜默跳過反思）。
- **`[Tool outcomes]` block（pre-rendered，reflection-only）**：在 `iterate()` 內，當 `is_reflection` 時 pre-render `tool_outcomes_block`（比照 `pulse_block` 字串模式），傳給 `render_mind`。格式：每工具一行 `- {tool}: {ok} ok, {fail} fail` + 最近 ≤3 筆失敗樣本（detail ≤100 chars）；block ≤20 行。例：
  ```
  [Tool outcomes since last reflection]
  - Shell: 3 ok, 1 fail — last fail: timeout after 60s
  - Recall: 5 ok
  ```
- `ReflectionMoment` 的 perception 文案（`_percep_body`）擴充：除既有「NoteMemory anything worth keeping」，加 nudge：「若有可重用的工具用法/陷阱，用 `NoteToolLesson` 記下來」。
- 由大模型本體在 idle 完成 distillation（gradient-free，無小模型），grounding = 真實 tool outcomes。

**Surface（慣性）= `[Tool habits]` block**：
- tool lessons 存 `tool_playbook.md`、已 index 進 FtsMemory。
- 新增 `tool_habits_search(memsearch, state, playbook_path: Path, top_k=2)`（`tool_memory.py`）：
  - **gate（spec-review #2）**：`if not state.tool_stats or not playbook_path.exists(): return []`——新裝機/無工具活動時不浪費一次 FTS round-trip。
  - **query（pinned，spec-review #2）**：`query = " ".join(list(state.tool_stats.keys())[:3]) + (" " + state.focus if state.focus != "idle" else "")`。**不**從 `recent_outputs.kind` 取（含 "Speech" 雜訊）。
  - **檢索（source-restricted，spec-review #1，非 post-filter）**：`await memsearch.search(query, top_k=2, source_prefix=str(playbook_path.resolve()))`。用 `FtsMemory.search` 既有的 `source_prefix`（`fts_store.py:179`，exact-resolve LIKE 過濾）。**廢除原本的 top_k=2 + endswith 後過濾**——一旦有 daily notes，後過濾幾乎 100% 回空。
- `mind_loop.iterate` 比照既有 `associative_search` 以 side-channel 取得 habits hits；`render_mind` 在 `associative_hits` 後新增參數 `tool_habits_hits: list[dict] | None = None`。
- `[Tool habits]` block **僅在有 hits 時 render**，top-2，每條一行 `- [situation] lesson`。解析 entry 用 `_parse_playbook_chunk(content) -> (situation, lesson) | None`（剝去 `## ` 行；讀 `[situation] …` 行 + 下一非空行為 lesson；無法解析則跳過）。

### § 3.3　順帶補上 Spec A 遞延項
- 補 `dispatch_one` 的 **runtime-error 分支**直接單元測試（Spec A 最終 review Minor，記錄掛鉤正落在此 outcome 上）。

---

## 4. 受影響檔案

- `src/dollos/mind/mind_state.py` — `ToolFailure` dataclass；`tool_stats: dict[str, dict[str, int]]` + `recent_tool_failures: deque[ToolFailure]`（maxlen 10）欄位；save/load **具體 snippet（spec-review #4）**：
  - `save_state`：`state_dict["recent_tool_failures"] = [asdict(f) for f in state.recent_tool_failures]`（`tool_stats` 為純 dict，`asdict(state)` 已涵蓋，無需特殊處理但須確認在輸出 dict 中）。
  - `load_state`：`recent_tool_failures=deque([_coerce(ToolFailure, f) for f in data.get("recent_tool_failures", [])], maxlen=10)` 與 `tool_stats=data.get("tool_stats", {})`，兩者都傳進 `MindState(...)` 建構。
- `src/dollos/mind/tool_memory.py`（新）— `record_tool_outcome(mind_state, name, result)`、`render_tool_notes(recent_tool_failures, now)`、`render_tool_outcomes(tool_stats, recent_tool_failures)`（reflection 用）、`tool_habits_search(memsearch, state, playbook_path, top_k=2)`、`render_tool_habits(hits)`、`_parse_playbook_chunk(content)`。
- `src/dollos/mind/mind_loop.py` — `_dispatch_tool` 後呼叫 `record_tool_outcome`；`iterate` 算 `is_reflection`、pre-render `tool_outcomes_block`、side-channel 取 `tool_habits_hits`、傳入 `render_mind`；新增 `REFLECTION_TOOLS` registry + lazy-cached reflection grammar（比照 `_safe_grammar`），`_active_tool_registry()`/`_active_grammar()` 納入 `is_reflection`（safe_mode 優先）。
- `src/dollos/mind/mind_prompt.py` — `render_mind` 新增 `tool_outcomes_block: str | None`、`tool_habits_hits: list[dict] | None` 參數；`[Tool notes]`（gated on recent failures）、`[Tool outcomes]`（reflection-only，由 caller pre-render 傳入）、`[Tool habits]`（gated on hits）三個 block；`ReflectionMoment` `_percep_body` 文案擴充。
- `src/dollos/tools.py` — `NoteToolLesson` 工具；**不**進 `MAIN_TOOLS`；新增 `REFLECTION_TOOLS = MAIN_TOOLS + [NoteToolLesson]`（或等價組合）。
- `src/dollos/cascade/tool_loop.py` — 無 code 改動；補 `dispatch_one` runtime-error 分支測試（測試在 tests/）。

---

## 5. 錯誤處理 / 邊界
- **記錄是觀測性，非 no-fallback 範疇**：`record_tool_outcome` 包 try/except，log 後續跑；記錄失敗絕不讓工具 dispatch 失敗或讓 turn 崩。
- `NoteToolLesson` 的檔寫入/index 失敗：比照既有 `NoteMemory`（append 是小寫入；index 失敗 log）。
- reflector nudge 失效（Doll 沒寫 lesson）= 可接受、不強制（[[ref_intrinsic-reflection-is-net-negative-without-external-grounding]]：反思需外部 grounding——這裡的 grounding 是真實 tool outcomes，故有效）。
- `tool_playbook.md` append-only：永不改寫既有 entry（[[ref_playbook-over-transcripts-ace-reasoningbank]]：避免 context collapse）。

## 6. 測試
- **Layer 1 記錄**：`record_tool_outcome` 對 None / success / fail 三種 result 正確更新 stats + recent_failures；fail 才 append ToolFailure；try/except 吞記錄錯誤不外傳。
- **`_dispatch_tool` 整合**：live dispatch 後 stats/failures 真的被更新（成功 +ok、失敗 +fail+failure）。
- **subagent 隔離負向測試（spec-review #8）**：跑 subagent 路徑（`run_tool_cascade`/`dispatch_tool_call`）後，**Doll 的 `tool_stats == {}` 且 `recent_tool_failures` 為空**——守住「記錄只在 live wrapper、不可被搬進 dispatch_one」。
- **`[Tool notes]` 渲染**：有近期失敗才出現、時間窗 aged-out、依 tool 去重後依時間 desc 取 5、截斷；無失敗時不 render。
- **`dispatch_one` runtime-error 分支**：tool.run 拋例外 → `ToolResult(success=False, detail 含 "runtime error")`（補 Spec A 遞延測試）。
- **`NoteToolLesson`**：append 到 tool_playbook.md（append-only，不覆寫既有 entry）、呼叫 index_file、回 `"lesson noted: …"`；entry heading 為 timestamp-only（無 `[k:v]` 軸標籤）。
- **reflection-only gating（spec-review #6）**：非 reflection turn 的 `_active_tool_registry()` 不含 `NoteToolLesson`；reflection turn 含它；safe_mode 優先（safe_mode 時即使 reflection 也不含）。
- **`[Tool outcomes]`（spec-review #5/#7）**：drained batch **含** `ReflectionMoment` 時才 pre-render（`[ReflectionMoment, UserSpoke]` 批次也要 true）；非 reflection turn 不出現。內容斷言含工具名 + `ok/fail` 次數 + 最近失敗 snippet（非僅「block 有 render」）。
- **`tool_habits_search`（spec-review #1/#2）**：以 `source_prefix` 限定到 `tool_playbook.md`（驗證 daily-note 雜訊不混入）、top-2；query 由 `tool_stats` keys + focus 組；`tool_stats` 空或 playbook 不存在時回 `[]`（不查）。`[Tool habits]` 僅 hits 時 render。
- **MindState round-trip**：save→load 保留 tool_stats + recent_tool_failures（含 field-tolerant drift）。
- 全套綠（含 Spec A 的 645 + 本 spec 新增）。

## 7. 風險
- **hot-path 延遲**：`[Tool notes]` gated 且精簡；`tool_habits_search` 是一次 FTS5 lexical 查詢（便宜）且 gated（無 tool_stats / 無 playbook 不查）。reflector 在 idle。整體對熱路徑影響應極小，但需以實機/既有 latency 意識把關。
- **FtsMemory 查詢序列化（spec-review）**：`FtsMemory` 以單一 `asyncio.Lock` 序列化所有查詢，故 `_derive_memory_hits` + `associative_search` + `tool_habits_search` 三者每 turn 是**相加**延遲（非並行）。`tool_habits_search` 的 gate 正是為了在無資料時省掉這第三次 round-trip。
- **`NoteToolLesson` re-index 整檔**：每次呼叫 `index_file(tool_playbook.md)` 會重新索引整個（單調成長的）檔；v1 規模可接受，與檔案歸檔一併列為未來。
- **playbook 品質**：依賴大模型 distillation 品質；append-only 可能累積雜訊（usefulness-counter 列為未來）。
- **記錄/狀態膨脹**：`tool_stats` 隨工具種類有界（工具數固定）；`recent_tool_failures` deque 有界（maxlen 10）。`tool_playbook.md` 單調成長——未來可加歸檔，v1 不處理。

## 8. 與 Spec A 的關係
Spec A 把工具層打乾淨並建立單一 `dispatch_one`；Spec B 在其上長出學習迴路。Layer 1 記錄掛在 live wrapper（dispatch_one 之後），Layer 2 複用既有 ReflectionMoment + FtsMemory + associative side-channel 機制——最大化複用，最小化新概念（符合「每個 plan 只加一個新概念」的增量開發準則，這裡的新概念是「工具記憶」）。
