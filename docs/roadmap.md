# DollOS Roadmap

每個 step 一個 plan（可能切多個 sub-plan）。每個 step 加 **一個新概念**，跑完即可 demo 該能力。

---

## 已完成

| Plan | 概念 |
|---|---|
| 1 — DollOS Skeleton | Python project + IPC WS server + 對話 round-trip stub |
| 2 — Memory SoT | sqlite-vec + FTS5 + RRF hybrid + character scoping |
| 3 — LLM Provider/Template | Provider / PromptTemplate ABC + LlamaCppProvider + Qwen3 templates |
| 4 — InnerVoice utility | recall(query) → "RECALL:\n..." (superseded by memsearch pivot) |
| Roadmap step 3 — VoM (memsearch-backed) | Merged |
| Roadmap step 4 — Event Loop (concurrent dispatcher + two-tier event model) | Merged |
| Roadmap step 5 — Inner Voice (minimal, summary-only) | Merged |
| Roadmap step 6 — Tool calling (Say + NoteMemory, pydantic) | Merged |
| Roadmap step 7 — Cascade (inner while-loop on tool fails) | Merged |
| Roadmap step 8 — Memory auto-write + Diary | Merged |
| Roadmap step 9 — Success-cascade + Shell | Merged |

---

## Roadmap

### 1. 確保 LLM 能用

把 IPC handler 從 stub 改成真的呼叫大模型：user 文字進來 → call LLMAdapter → stream token 回 IPC TextChunk → 送 TurnEnd。System prompt 暫時寫死（e.g. `"You are Doll."`）。

**Demo**：使用者打字，Doll 用大模型回應。沒有記憶、沒有人格、沒有工具。

### 2. 第一版 system prompt + rendering

加 jinja2 dep。`dollos.prompts` 模組：PromptRenderer + RenderedPrompt(system, user, prefill)。內建預設 templates 目錄。寫死的 default `doll_character.jinja`（Doll 的人格描述模板）。把 Plan 4 InnerVoice 寫死的 recall prompt 搬進 `iv_recall.jinja`。IPC handler 改用 PromptRenderer 渲染 system prompt。

**Demo**：行為不變，但 prompt 渲染走 template。基礎設施。

### 3. VoM

IPC handler 在送 user input 給大模型前，先 call InnerVoice.recall(user_input) → 結果接進 prefill（`<think>\n{recall}DECISION: `）。**只讀 memory 不寫**。

**Demo**：Doll 能引用既有 memory（手動填 memory 後測），但還不會自動寫新 memory。

### 4. 跑通 event loop  ✅ Merged

Two-tier event model（`RawEvent` ABC + `UserTextEvent` / `DollEvent` perception）+ `EventDispatcher`（sync `dispatch()` spawn `asyncio.Task` per event，無 worker / queue / mutex）。IPC handler 變薄，recall + 大模型 stream 邏輯 lift 進 `EventDispatcher._respond`。`DollEvent.perception` 餵大模型 `user` role；step 4 用 stub passthrough，step 5 Inner Voice 真正 perceive。

Smoke-tested：memsearch + IV plain + 大模型 stream 端對端通；prefill `GOAL:` 觸發 think loop，改 `DECISION:` 後乾淨收 `</think>`。

**Demo**：行為跟 step 3 一致；多 client 真的並行（依賴 llama.cpp `--parallel`）；為 step 5 Inner Voice + step 6 tool / step 9 subagent 的 RawEvent 注入點鋪好。

### 5. Inner Voice  ✅ Merged

Step 5 minimal scope: Instinct ABC + SmallModelInstinct + iv_summary.jinja + EventDispatcher STATE-block injection + Kernel build_instinct factory. Per-event small-model call produces rolling natural-language summary; non-empty summary prepends `STATE:\n{summary}\n\n` to big-model prefill before existing RECALL block.

Smoke-tested: 3-turn conversation; rolling summary persists across turns; big model references prior context. first_instinct / emotion deferred (YAGNI; emotion goes to big-model think).

**Demo**：行為跟 step 4 一致 + 多了「Doll 持續摘要」的延續感；下個 step 是 step 6 Tool calling。

### 6. Tool calling  ✅ Merged

Step 6 minimal scope: pydantic Tool models (Say, NoteMemory) with run(ctx); ToolStreamParser state machine; Qwen3ThinkingTemplate `# Tools` system-prompt section; LLMAdapter tools= plumbing; EventDispatcher parser-driven _respond; Kernel wires memory_root + memsearch.

Smoke-tested: 3-turn conversation; output via Say tool only (no naked-text leak); NoteMemory writes daily markdown + memsearch.index_file synchronously. recall tool / cascade / permission / streamable / fast deferred.

**Demo**：Doll 透過 tool 講話 + 寫 memory；下個 step 是 step 7 Reflex + cascade。

### 7. Reflex + pre + post

**Re-cut to step 7 = Cascade only**. Reflex deferred to its own research+brainstorm; review dropped (architecture conflict with Self-First).

Step 7 minimal scope: `_dispatch_tool_call` returns `ToolCallFailure | None`; `_respond` is an inner while-loop in the same asyncio task; tool failures (validation / unknown / runtime) are formatted into a perception narrative for the next big-model invocation in the same turn. Iteration count surfaced as "第 N 次重試". `MAX_CASCADE_DEPTH = 50` runaway cap. `scaffolding.jinja` adds meta-rule about multi-try / change approach / stop. Only fail-cascade — success-cascade deferred to step 9 returning tools.

**Demo**：Doll 看得到自己 tool call 失敗 → 修正 args / 換 tool / 放棄；turn 行為對 user 透明（只看到最終正確輸出）。

下個 step 是 step 8（自動寫 memory）或 reflex research（依時序選）。

### 8. Memory（自動寫）

**Re-cut**: roadmap 原文「v1 寫全部、無顯著性過濾」採折衷——transcript 走 ephemeral 路徑（同日 recall 可見），LT memory 由 Doll 自己寫日記產生。

Step 8 minimal scope: `memory_writer.append_transcript` 寫 `[HH:MM role] X` 到 `data/memory/transcripts/{date}.md`（dispatcher 在 `_handle` finally 寫 user，Say.run 寫 doll）。memsearch 索引兩個目錄。新 `WriteDiary` pydantic tool 寫 markdown section 到 `data/memory/shared/{date}.md`。新 `DiaryEvent` RawEvent + dispatcher routing；kernel `_diary_scheduler` 每日 23:00 fire；`_drain_diary_sink` 內部消費。情緒走大模型 think 自由發揮，無新 emotion infrastructure。

**Demo**：對話自動進 transcript（即時可 recall），每日固定時間 Doll 醒來寫日記（含情緒），隔日 recall 引用日記反思。

### 9. Success-cascade + Shell  ✅ Merged

**Re-cut**: 原 roadmap step 9 為 Subagent。實際排程改：先做 success-cascade + Shell（讓 Doll 透過 shell 操控環境，並把 cascade 從 fail-only 升級成 success+fail unified）；Subagent 留到之後。

Step 9 minimal scope: Tool.run 簽名 `-> str | None`（None = side-effect tool 不 cascade，str = cascade with content）。`ToolCallFailure` 升級成 `ToolResult(tool_name, success, detail)`，success/fail 共用 cascade 路徑。新 `Shell` returning tool（fresh subprocess via asyncio.to_thread，cwd=`data/`，default 30s/max 300s timeout，stdout+stderr 合併，8000-char head/tail truncation）。trust-only（無 permission gate / 無 sandbox）。

**Demo**：Doll 透過 Shell 執行命令、看結果、接續講話；cascade 同 turn 多輪正常。

下個 step 是 step 10（Skills system — entry/body 分離 + InvokeSkill returning tool）。

### 10. Character

`.doll` v3 minimal schema：`manifest.json` + `prompts/character.jinja`。character.jinja 覆寫 #2 的 default `doll_character.jinja`，渲染時吃 ctx（{{ S }} / {{ tools }} / {{ self_state }} / ...）。CharacterPack dataclass + load_character_pack()。`[character] default_pack` config。範例 gura.doll。

**Demo**：Doll 真有人格，可換包換靈魂。

---

## 之後（未排序）

- Character 切換 / 熱重載
- Self-First 完整（self_history、emotional_residue、慢變演化）
- UI Cubism 渲染 + lip sync
- Voice pipeline（TTS / ASR / audio WS）
- Phone App + system assistant + KWS + VAD + speaker ID + PTT + 鎖定畫面
- Drone（持久 definition + cron + UI）
- Reflex 真規則庫（自然語言編譯 + UI）
- Phone Tier B/C/D（A11y / Shizuku / Root）
- 構想 / 長期：Twin Mode、Robot vision、AI 視覺、Galgame 介面、Latency calibration、Snapshot S、Multi-thread、Interrupt、Tick/idle、...

完整候選見 `docs/feature-list.md`。
