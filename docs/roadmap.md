# DollOS Roadmap

每個 step 一個 plan（可能切多個 sub-plan）。每個 step 加 **一個新概念**，跑完即可 demo 該能力。

---

## 已完成

| Plan | 概念 |
|---|---|
| 1 — DollOS Skeleton | Python project + IPC WS server + 對話 round-trip stub |
| 2 — Memory SoT | sqlite-vec + FTS5 + RRF hybrid + character scoping |
| 3 — LLM Provider/Template | Provider / PromptTemplate ABC + LlamaCppProvider + Qwen3 templates |
| 4 — InnerVoice utility | recall(query) → "RECALL:\n..." (branch 留著未合) |

---

## Roadmap

### 1. 確保 LLM 能用

把 IPC handler 從 stub 改成真的呼叫大模型：user 文字進來 → call LLMAdapter → stream token 回 IPC TextChunk → 送 TurnEnd。System prompt 暫時寫死（e.g. `"You are Doll."`）。

**Demo**：使用者打字，Doll 用大模型回應。沒有記憶、沒有人格、沒有工具。

### 2. 第一版 system prompt + rendering

加 jinja2 dep。`dollos.prompts` 模組：PromptRenderer + RenderedPrompt(system, user, prefill)。內建預設 templates 目錄。寫死的 default `doll_character.jinja`（Doll 的人格描述模板）。把 Plan 4 InnerVoice 寫死的 recall prompt 搬進 `iv_recall.jinja`。IPC handler 改用 PromptRenderer 渲染 system prompt。

**Demo**：行為不變，但 prompt 渲染走 template。基礎設施。

### 3. VoM

IPC handler 在送 user input 給大模型前，先 call InnerVoice.recall(user_input) → 結果接進 prefill（`<think>\n{recall}GOAL: `）。**只讀 memory 不寫**。

**Demo**：Doll 能引用既有 memory（手動填 memory 後測），但還不會自動寫新 memory。

### 4. 跑通 event loop

Event ABC + UserTextEvent + 各 history item dataclass。Event Queue（asyncio.Queue）+ DollLoop 主迴圈。IPC handler 改 push UserTextEvent 進 queue；DollLoop pop event 跑「recall + LLM call + stream」同樣邏輯。

（DollOS / kernel.py rename 已在 step 2 處理）

**Demo**：行為不變，內部結構變 event-driven。為後續 plan 鋪路。

### 5. Inner Voice

Instinct ABC + SmallModelInstinct。每 event 一次小模型 call 產 **first_instinct + emotion + summary** 三項。DollState (S) = summary 純文字。S 接進大模型 prefill（與 recall block 並列或合併）。

**Demo**：Doll 多了「內心反應 / 情緒 / 持續摘要」，回應更有連貫感。

### 6. Tool calling

Tool ABC + ClassVar `name` / `permission` / `feedback` / `fast` / `streamable`。ToolRegistry + permission-checked execute（兩級權限）。第一批 tools：say（external、streamable）+ note_memory（external）+ recall（internal）。Template 擴 `render_tools` / `parse_stream` / `format_tool_result`（Qwen3ThinkingTemplate native `<tool_call>` JSON）。**say 變 tool call**（結構統一）。大模型 single-round；tool 執行 sync；結果不回大模型（cascade 留 #7）。

**Demo**：Doll 會用 tool — 講話 / 寫 memory / 主動 recall。但每輪只一次大模型 call。

### 7. Reflex + pre + post

完整 bracket loop。Instinct.process() 加 reflex_calls 輸出（規則命中 → external whitelist tool）。Instinct.review() 階段（approved_calls, continue_thread）。ToolExecutedEvent cascade（reflex / 大模型 approved 都產 event 進 queue）。MAX_ITERATIONS backstop。Doll 自決停止（review continue_thread = False）。

**Demo**：Doll 能多輪反應自己動作（recall result → 接續），有 small-model 守門。Reflex 規則庫 stub（具體規則之後）。

### 8. Memory（自動寫）

UserTextEvent 進 loop 時自動寫 memory（user 原話保真）。Assistant utterance 寫 memory（v1 寫全部、無顯著性過濾）。

**Demo**：對話記憶完整 — Doll 知道你說過什麼、自己說過什麼。

### 9. Subagent

spawn_subagent tool（external、fast=False）。Inline definition + 隔離 session + 預算（max_tokens / max_wall_clock_s）。fast=False async pattern：execute 立即回 dispatched-ack，subagent 跑完自己 push SubagentResultEvent 回 queue。

**Demo**：Doll 能派分身做任務，結果非同步回流。

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
