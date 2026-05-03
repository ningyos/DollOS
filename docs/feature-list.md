# DollOS Feature List

工作底稿 — 用來重新切 plan。可隨意編輯、重排、補刪。

**Status legend**
- ✅ 完成（plan merged）
- 🔄 部分完成（branch 留著未合）
- 📝 spec 提過
- 💭 對話討論過
- 🔗 依賴

順序大致從「應先做」到「之後再說」，但不是承諾 — 真正的 plan 切法看編輯後的決定。

---

## 1. 已完成基礎

- ✅ DollOS Skeleton（Python project + uv + IPC WS server + 對話 round-trip stub）
- ✅ Memory SoT（sqlite-vec + FTS5 + RRF hybrid + character scoping + Embedder ABC + LlamaCppEmbedder）
- ✅ LLM Provider/Template 解耦（Provider ABC、PromptTemplate ABC、ComposedLLMAdapter、LlamaCppProvider、Qwen3ThinkingTemplate、Qwen3PlainTemplate）
- 🔄 InnerVoice utility：`recall(query) → "RECALL:\n..."`（Plan 4 branch 未合）

## 2. Prompt Rendering Layer

- 💭 加 jinja2 dep
- 💭 `dollos.prompts` 模組：PromptRenderer 類別 + RenderedPrompt(system, user, prefill) dataclass
- 💭 內建預設 templates 放 `src/dollos/prompts/templates/`
- 💭 把 Plan 4 InnerVoice 寫死的 recall prompt 搬進 `iv_recall.jinja`
- 🔗 是 character pack 的前置（character 用 jinja template 不是 raw text）

## 3. Memory 整合對話

- 💭 每個 UserTextEvent 自動寫 memory（user 原話保真）
- 💭 InnerVoice.recall() 接進大模型 prefill
- 💭 每輪 assistant 回應自動寫 memory（顯著性過濾留之後）

## 4. DollLoop 骨架

- 📝 Daemon class rename → DollOS（file `daemon.py → kernel.py`）
- 📝 Event ABC + UserTextEvent + 各 history item dataclass
- 📝 Event Queue（asyncio.Queue）
- 📝 DollLoop class（main run loop）
- 📝 IPC handler 改 push event 進 queue
- 📝 ToolExecutedEvent

## 5. Tool 系統（核心 tools）

- 📝 Tool ABC + ClassVar `name` / `permission` / `feedback` / `fast` / `streamable`
- 📝 ToolRegistry + permission-checked execute
- 📝 Internal vs External 兩級權限
- 📝 say tool（external，串流到 IPC）
- 📝 note_memory tool（external）
- 📝 recall tool（internal，包 InnerVoice.recall）
- 📝 say 變 tool call（結構統一）

## 6. Inner Voice 進 loop

- 📝 Instinct ABC `process(event, history, S) → InstinctOutput`
- 📝 SmallModelInstinct 實作（一次小模型 call 產 first_instinct + emotion + summary）
- 📝 build_instinct() factory
- 📝 DollState (S) = Inner Voice 持續摘要（v1 純文字）

## 7. Bracket Loop（Post IV review）

- 📝 Instinct ABC 加 review 階段
- 📝 ReviewOutput dataclass（approved_calls, continue_thread）
- 📝 ToolExecutedEvent cascade（reflex / 大模型 approved 都產 event 進 queue）
- 📝 大模型 single-round（無 inner ReAct loop；ReAct 從 cascade emergent）
- 📝 Doll 自決停止（post IV `continue_thread = False`）

## 8. 大模型 Tool-Call 整合

- 📝 PromptTemplate 擴 render_tools / parse_stream / format_tool_result
- 📝 Qwen3ThinkingTemplate 實作 tool 三方法（Qwen3 native `<tool_call>` JSON）
- 📝 ToolCall / ParsedItem 資料型別

## 9. Character Pack（基於 prompt rendering）

🔗 **依賴 Prompt Rendering Layer**

- 📝 `.doll` v3 minimal schema：
  ```
  manifest.json
  prompts/
  └── character.jinja        # 必，渲染成 system prompt
  ```
- 📝 `.doll` v3 full schema（+ voice / kws / cubism / lessons / scene / thumbnail）
- 📝 character.jinja 渲染時吃 runtime ctx（{{ S }}, {{ tools }}, {{ emotion }}, ...）
- 📝 預設 templates 放 daemon 內建；character pack 提供同名 `.jinja` 即覆寫
- 📝 character pack 可選 override：`prompts/instinct_overrides.jinja` / `recall_overrides.jinja`
- 📝 CharacterPack dataclass + load_character_pack()
- 📝 Daemon config: `[character] default_pack`
- 📝 範例 gura.doll
- 📝 v2 → v3 不向下相容
- 📝 Character 切換 / 熱重載

## 10. Reflex / 規則

- 📝 Reflex tool whitelist（external 子集）
- 📝 Inner Voice 規則命中 → 出 reflex_calls
- 📝 自然語言規則編譯（一次性 LLM classify → 內部結構）
- 📝 Pattern match 執行（零 LLM 延遲）
- 📝 規則 UI 編輯
- 📝 Doll 對話中說「以後別這樣」→ 規則寫入

## 11. Self-First / mood / state

- 📝 Self-memory schema（preferences / habits / relations / emotional_residue）
- 📝 Mood baseline + 慢變演化模型
- 📝 SELF_STATE 進 character.jinja ctx
- 📝 Inner Voice summary 擴 self-traits slice
- 📝 self_history 記錄

## 12. Subagent

- 📝 spawn_subagent tool（即時派出隔離 session）
- 📝 Inline definition（不存檔）
- 📝 工具白名單（subagent 預設不讀 Memory SoT）
- 📝 預算：max_tokens、max_wall_clock_s
- 📝 結果直接回 Doll 同 turn（不過 Instinct digest）
- 📝 fast=False async tool 介面（fire-and-forget，結果以 SubagentResultEvent 回流）

## 13. UI（Tauri + Cubism Web）

- 📝 Tauri shell（Win/Mac transparent overlay；Linux 一般視窗）
- 📝 Cubism Web SDK 整合
- 📝 Chat 視窗
- 📝 System tray
- 📝 Hotkey
- 📝 Localhost WS client
- 📝 Lip sync 渲染
- 📝 Galgame 式介面（Doll 丟選項給 user）
- 📝 App Drawer

## 14. Voice Pipeline

- 📝 ASR @ daemon（whisper.cpp / sherpa-onnx）
- 📝 TTS @ daemon（Piper VITS distilled）
- 📝 Lip sync stream（phoneme/viseme）daemon → UI/App
- 📝 Audio chunked WS（Opus 編碼）
- 🔄 Wake word 訓練 pipeline（既有 wake_word_training/）
- 🔄 TTS distillation（fish-tts → VITS，既有）

## 15. Phone App（Android）

- 📝 Cubism Java SDK
- 📝 VoiceInteractionService 註冊
- 📝 Audio uplink / downlink WS
- 📝 Network WS client
- 📝 KWS opt-in（每角色 wake_word.onnx）
- 📝 VAD（silero）
- 📝 Speaker ID（ECAPA-TDNN）
- 📝 鎖定畫面互動 + sudo 式安全 + session 對話
- 📝 緊急停止（電源菜單 AI Stop）
- 📝 PTT（長按電源鍵）
- 📝 鏡頭使用每次取得主人許可
- 📝 AI 設定介面對 AI 完全隔離
- 📝 KWS 靈敏度 UI

## 16. Drone

- 📝 Drone Definition Store（持久）
- 📝 Cron-like trigger
- 📝 Schedule / external trigger / 條件啟動
- 📝 新建 drone 需 user 一次確認
- 📝 結果回 event queue（過 Instinct）
- 📝 UI 編輯 / 撤銷 / 審計

## 17. Phone Tier B/C/D 整合

- 📝 A11y / Shizuku / Root 模組
- 📝 系統設定切換（WiFi/BT/勿擾）
- 📝 跨 app 操作
- 📝 螢幕截圖 / accessibility tree（UI 感知）
- 📝 通知 / 來電 / 隱私
- 📝 危險操作指紋認證
- 📝 智慧電源管理

## 18. Robustness / Safety

- 💭 MAX_ITERATIONS backstop（per thread 上限）
- 💭 Single-thread enforcement + user_input_buffer（並發 interleave）
- 💭 History windowing in prompt（IV 看 N₁ / 大模型看 N₂ + S）
- 💭 History 真壓縮（按重要性 prune）
- 💭 Tool execute 失敗的 ToolExecutedEvent error 表示
- 💭 LLM crash / timeout 處理
- 💭 顯著性過濾（auto memory write 規則）
- 💭 FTS5 special char escape

## 19. 構想 / 長期

- 📝 Twin Mode（一機兩 Doll）
- 📝 Ingest pipeline + task queue
- 📝 Robot vision（USB-C 操控實體機器人）
- 📝 AI 視覺理解（螢幕 + 相機）
- 📝 AI 透過手機操控電腦
- 📝 AI 語音喚醒鬧鐘
- 📝 學習主人生活習慣
- 📝 Latency calibration（setup wizard）
- 📝 Snapshot DollState 進硬碟
- 📝 Multi-thread / thread isolation
- 📝 Interrupt model（user 打斷 Doll mid-utterance）
- 📝 Tick / idle behavior（時間流動 → 主動發話）
- 📝 Cross-character memory share

---

## 已知依賴

- **Character Pack** ← Prompt Rendering Layer
- **Inner Voice 進 loop** ← Memory 整合對話 ← Plan 4 InnerVoice utility
- **Bracket Loop** ← Inner Voice 進 loop
- **say 變 tool call** ← Tool 系統 + Bracket Loop
- **Self-First** ← Inner Voice 進 loop（擴 summary slice）
- **Reflex 真規則** ← Phone App / Voice / Drone（event 來源齊才有規則寫頭）
- **UI Cubism** ← UI 殼
- **Phone Tier B/C/D** ← Phone App
