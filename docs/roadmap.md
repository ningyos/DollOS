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
| Roadmap step 10 — Skills system | Merged |

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

**Re-cut**: 原 roadmap step 10 為 Character pack。實際排程改：先做 Skills system（讓 Doll 能累積 procedural memory）；Character pack 留到之後。

Step 10 minimal scope: Skill 兩檔分離——`data/memory/skills/<name>.md`（entry，frontmatter `name` + 短 prose description，由 memsearch 索引、進 RECALL）+ `data/memory/skill_bodies/<name>.md`（body，完整 instructions，不索引）。新 `InvokeSkill(name)` returning tool 載入 body 進 cascade（吃 step 9 success-cascade）。`scaffolding.jinja` 加 skill convention 段教 Doll 怎麼用。Doll 用 Shell tool 寫新 skill。

Smoke test 結論：**infrastructure 完整通過 unit test（174 → 178 tests），但 real-model behavior 退化**——加長 scaffolding 後 Qwen3.6 35B 變得更不可靠地遵守 tool_call 格式（步 6 §10.4 既有問題加重）。需要 Character pack（step 11+）幫 Doll 建立更紮實的角色身份才能改善 tool-using 行為。

**Demo 預期**：對話中 Doll 寫 skill；隔輪 user 觸發類似情境，RECALL 帶到 entry，Doll 主動 InvokeSkill 讀 body 跟著做。**Demo 實際**：機制可達成（unit test 證明），但要 Doll 在真實對話穩定觸發此流程需後續 Character pack 改善。

下個 step 候選：Character pack（最高優先——直接修 model 行為）/ wake gating / Subagent / Voice pipeline。

### 11. Prompt-compact + grammar wiring  ✅ Merged

**Pivot**：原預期 step 11 直接做 Character pack，實際先做 prompt-compact 跟 grammar wiring，因為 step 5/10 的 model 退化問題不是 character 內容夠不夠就能修——是格式洩漏 / tool 名幻覺 / JSON 形狀錯。先把結構鎖緊再談人格。

Step 11 範圍：
- character.jinja 改寫：移掉 few-shot 對話範例（model 會模仿成「主人說：xxx」假轉錄）；加「絕對不做」list（不寫 ReAct 標籤、不模擬 tool 結果、不寫假對話）。
- 移除 STATE/RECALL prefill 注入：dispatcher hard-code `prefill = ""`。IV.recall 仍跑（memsearch upkeep）但結果不進 think block。**修了無限轉錄續寫 bug**。
- B4-typed GBNF wiring（`build_qwen3_think_tool_grammar`）：think 鎖成 SEEN/INTENT/TOOL 三 field + per-tool typed JSON envelope（field name 鎖死）；`LlamaCppProvider.stream` 加 per-request `grammar` 參數；dispatcher 每 turn build grammar。
- JSON string codepoint deny list：`[^"\\“”‘’「」『』]` 防止 model 用中文 closing quote 假關 JSON（byte-level grammar 漏洞，T8 smoke 親見）。
- 5 個 stale RECALL/STATE prefill test 清掉。

**Demo**：T1-T8 smoke 5/8 visible 強角色（vs 自由 sampling 4-5/8 + 大量格式洩漏 / 幻覺 tool name）；零 malformed JSON warning；think tokens 縮 ~15× per techreport §3 E1。

**已知遺留**（grammar 範疇外，下個分支處理）：
- T4/T5/T8 InvokeSkill 幻覺：fresh data 下 model 把所有未知任務都先當 skill 查（`Errno 2 No such file or directory`）。character / scaffolding 把 skill lookup 寫太搶眼。
- Grammar `tool-name` 跟 `tool-call` 沒 cross-link：think 寫 `TOOL: InvokeSkill` 但 emit `<tool_call>{"name":"Say"}` 仍合法。要改成 5 個 production rule 展開（commit 變數）。
- Scaffolding 還在洩漏 STATE/RECALL 概念：prefill 已移除但 system prompt 還提 → model 偶爾在 SEEN field 幻覺 `STATE: drink_preference: ...` 這種捏造結構。
- Cascade 後 Say 弱化：T4 Shell 跑完，第二輪 perception 是「你 call 了 Shell tool 成功，回傳：...」，model 沒 forward 結果給用戶（Say 只 emit「嗯?」）。`_format_results_perception` 沒指引要 Say。
- Cascade 自我崩潰：T8 第 3 次重試時 model 自己抱怨「這已經是第三次了」。MAX_CASCADE_DEPTH=50 過寬，缺 emotion-aware 上限。
- T2 fabrication 偶發：fresh data 時開玩笑（好），有 stale data 時偶爾編造「主人喜歡美式咖啡」。memsearch miss → model 取捨不穩。需要明確「miss → 不瞎掰」mechanism。
- dispatcher 啟動 cwd-relative：daemon 從非 worktree 目錄啟動會用錯 `data/`。Config `root` 設計小坑。

下個 step 候選（按優先序）：InvokeSkill 幻覺修復 / Character pack / cascade Say 強化 / Subagent。

### 12. Memory wire format pivot — RAG context + Recall tool  ✅ Merged

**Pivot 動機**：step 11 砍掉 STATE/RECALL prefill 注入後，IV.recall 跑完結果丟掉、Instinct.process 同樣浪費。深一層發現：**LLM 訓練分佈裡根本沒有「ReAct-style 結構化記憶 prefill」這種東西**。業界主流走兩條已訓練通道——RAG context in user message（LangChain / ChatGPT memory）+ tool-based memory（Anthropic memory tool / Letta）。VoM 原 wire format（自定義 STATE/RECALL 標籤塞 think prefill）是 0% 訓練覆蓋的孤兒做法。

**架構保留**：兩層模型分工、Instinct / InnerVoice class、memsearch SoT、`prefill` 機制（template / adapter 仍接受參數，將來不同 wire format 可用）— 沒動。**只換 wire format**。

Step 12 範圍：
- `inner_voice.py`: `recall()` 返回值剝掉 `RECALL:\n` 前綴跟 `(no relevant memories)` wrap，純 plain filtered text；空回 `""`。
- `iv_recall.jinja`: small-LLM 不再產 `RECALL:` 前綴。
- `dispatcher.py`: 移除兩個 `Instinct.process()` call（Instinct class 留著供 wake-gating / reflex 將來用）。把 IV.recall 結果包成 `[Memory context]\n{text}\n\n[Message]\n{perception}` 進 user msg；空 recall 仍出 `(no relevant memory)` block。
- 新 `Recall` pydantic tool（`tools.py`）：`Recall(query)` 走 raw memsearch（top-5），不二次 filter（baseline 已 filter）。Grammar generator 自動納入。
- `scaffolding.jinja`: 加 `# Memory` section 教 Doll 用 `[Memory context]` + `Recall` tool；`# Skills` 段 RECALL 字眼改寫成 `[Memory context]`。

**Demo**：T1-T8 smoke **7/8 visible**（vs step 11 之 5/8）；零 ERROR、零 malformed JSON、**零 InvokeSkill 幻覺**。
- T2 出現 **Self-First**：「我最近超愛喝冰美式」反問用戶喜好（spec §8 預期效果首次自然浮現）
- T7 跨 turn RAG：引用 T6 剛寫進的 NoteMemory（「主人的知識庫升級了」）
- T4/T5 乾淨用 Shell（step 11 必爆的 InvokeSkill 幻覺消失，因 scaffolding 把 skill 用法明確跟 `[Memory context]` 綁定）

**仍遺留**（不在 step 12 範圍）：
- T8 cascade Say 弱化：「跑完了？可是結果呢？」— `_format_results_perception` 沒引導 model forward tool 結果。本質 cascade 設計問題。
- Grammar `tool-name` ↔ `tool-call` cross-link 缺漏（step 11 遺留）
- T8 cascade 自我崩潰防呆缺漏（step 11 遺留）

下個 step 候選（按優先序）：cascade Say 強化（最直接影響 UX）/ Subagent / Wake gating / Voice pipeline。

#### Post-merge 發現（2026-05-08 deterministic smoke, temp=0/top_k=1）

合併後跑 deterministic smoke（暴露 model real baseline，非 sampling lucky path）→ **3/8 vs 上次 sampling smoke 7/8**。差距全部出在 T2/T4/T5/T7/T8。揭露：

- **InvokeSkill 幻覺是 deterministic baseline 行為**，不是偶發。step 12 之前 worktree smoke 的 7/8 是 sampling luck（temperature=0.6 偶爾躲過幻覺路徑）。
- **新加 `Recall` tool 沒解 InvokeSkill 偏好**：model 看到「未知任務」deterministic 仍先猜 skill 檔名 call InvokeSkill，不會自動切到 Recall 或 Shell。Recall tool 設計沒錯，是 scaffolding 引導不夠強。
- **Cascade 灌爆**：T4 連 4 次 InvokeSkill ENOENT（猜不同檔名 `system/initialization.md` / `debug_shell_errors.md` / `create_skill.md`）；用戶端看到 4 個 ERROR 才等到 Say。`MAX_CASCADE_DEPTH=50` 跟「emotion-aware cap」都缺。
- **Tool error format 太低階**：`Errno 2: No such file or directory: 'data/memory/skill_bodies/X.md'` 對 model 沒指引性，只會繼續猜下一個檔名。
- **T7 echo bug**：「我剛才說了什麼」回 T5 的錯誤回應當答案——transcript 撈到的是「最近一句 Doll Say」即可，無語意過濾。

**結論**：step 11/12 已列「InvokeSkill 幻覺」為 #1 待修，這次 deterministic smoke **量化確認嚴重程度**——sampling 模糊了真實基線，必須優先解。

下個 step（修正版優先序）：
1. **InvokeSkill / cascade 失敗治理**：scaffolding 把 InvokeSkill 改成 conditional（只在 `[Memory context]` 看到 entry 才能用）+ InvokeSkill 失敗 message 改成有指引性 + cascade depth 收緊到 5 + 同 tool 連續失敗 ≥3 直接斷
2. cascade Say 強化（forward tool 結果）
3. Subagent / Wake gating / Voice pipeline（功能擴張，等 baseline 穩定）

### 13. Cascade robustness — multi-message + skills audit + character trim  ✅ Merged

**動機**：step 12 的 deterministic smoke 揭露三個獨立病症：(1) InvokeSkill 幻覺 deterministic baseline、(2) cascade Say 弱化、(3) T2 / T7 model 詮釋偶發失敗。Step 13 一次解決前兩個，第三個需 cross-turn history（留下個 step）。

Step 13 範圍（4 個 commit + 1 個探索 log）：

**a. cascade governance + Hermes skills audit** (`9a95376`)
- `MAX_CASCADE_DEPTH 50 → 5`、同 tool consecutive **failure** counter（≥3 → break + ErrorMsg）
- `InvokeSkill.run` ENOENT short-circuit → 列出實際存在的 skills 的 corrective str（不再 raise）
- `scaffolding.jinja` 把整個 `# Skills` section 包進 `{% if available_skills %}`：dispatcher 讀 `data/memory/skills/*.md` glob 傳 sorted stems。**沒 skill 安裝時 InvokeSkill / skill_bodies 概念完全不出現在 system prompt**（Hermes #1 smoking gun）
- 結果：T4/T5 的 InvokeSkill 幻覺**完全消失**

**b. multi-message conversation history within turn** (`7794bbe`)
- 廢掉 single-shot perception re-render；改 multi-message ChatML：原 user perception 持續在 `messages[0]`，每 cascade iter 加 assistant raw emit + per-result `<tool_response>` user message
- 新 `Qwen3ThinkingTemplate.render_messages`、`LLMAdapter.stream_messages`（legacy `stream_completion` 留給 InnerVoice / Instinct）
- recall + scaffolding 從 per-iter 變 per-turn render
- 結果：T4/T5 從 1/3 → 3/3、T8 build_skill 從 0/3 → 2/3、T7 偶現 cross-turn recall

**c. character + scaffolding 求知慾 + LARP trim** (`85a082a`)
- character.jinja：移除「9000 歲 / 老氣橫秋 / 黏 / 逗主人」LARP 助長條、`# 個性` 7 → 4 bullets、新增「不 LARP」反向 anchor、修 stale `STATE/RECALL` references
- scaffolding `# Memory`：加「主人問你不確定的事」fallback 三步流程（Recall 換 keyword → 直接問用戶 → NoteMemory）
- 結果：T2 命中率 0/3 → 2/3 with 穩定好奇行為（「沒記下來，主人喜歡什麼？」）；T2 平均回應長度從 ~100 字降到 ~30-40 字；零「血腥瑪莉開玩笑啦」式 LARP 填充

**d. exploration log** (`01ee726`) - `docs/research/cascade-governance-exploration.md`
- 完整記錄探索失敗的 4 個方向：budget pressure、naive YES/NO judge、wrap-up iter、5-flag sanity guard
- 全部 revert，原因見文件
- 留作未來 reference 避免再踩同坑

**Smoke**：3 sampling runs，平均 **~7/8**（vs step 12 結束時 6/8）；零 InvokeSkill ENOENT；零 ERROR；T2 / T8 從幾乎不通變多數通

**已知遺留**（step 13 未解，下個 step 候選）：
- T2 / T7 偶發誤判：fresh data 時 model 看不到 prior turn，靠 memsearch 撈不可靠 → 需要 **cross-turn conversation history**（架構級，留下個 step）
- T7 偶發 cascade exceeded：MAX_CASCADE_DEPTH=5 對 cross-turn recall 有時太緊
- T5 在 `data/` 內跑 `ls data` 誤判：Shell tool 沒提示 cwd
- T8 偶發 cascade exceeded：build_skill 多步任務需 4-5 iter

下個 step 候選（按優先序）：
1. **Cross-turn conversation history**（最高槓桿，解 T2 / T7 不穩定根因）
2. cascade depth 拉到 20 + 同 tool any-outcome counter（小改動，從 exploration log 把 budget pressure 那塊改良後上）
3. Subagent / Wake gating / Voice pipeline / Character pack

### 14. Episodic memory + uncapped cascade + REVIEW think field  ✅ Merged

**動機**：step 13 後 T7 / T8 仍偶發 cascade exceeded（depth=5 太緊）；本質問題不是 depth cap 太小，是 model 沒辦法跨 turn 看見過去做了什麼。Cross-turn 的 wire format 又跟「DollOS 沒 session 概念」哲學衝突。**正解：cascade end 時小模型 compact 成一句話，append 到 rolling buffer，下個 cascade 看見**——episodic memory 不是 conversation transcript。

但加完 rolling 後，移除 cap 重跑 smoke 揭露**真正的失敗模式**：96 次 inference 中 model 完全相同的 3 行 think 重複 ~70 次，**B4-typed grammar 沒給 self-reflection 的 syntactic 空間**。Surgical fix：think 加第 4 個 REVIEW 欄位。

Step 14 範圍（一個 commit + plan docs，merge `5b6cd79` → `9c79375`）：

**a. Rolling cascade compact**（episodic memory）
- 新 `iv_compact.jinja`（small-LLM 1-2 句第一人稱過去式）
- `Instinct.compact_cascade()` 新方法
- `EventDispatcher._rolling: list[str]` daemon-life buffer
- `_respond` 開始：`[Recent activity]\n- ...\n\n` block prepend 到 user message（在 `[Memory context]` 上方）
- `_respond` 結束（不分 cascade exit reason）：compact + append，try/except 防 crash turn

**b. MAX_CASCADE_DEPTH 移除**
- 整個 hard cap 拿掉（包括 iteration counter + depth check）
- 同 tool 連續失敗 ≥3 次的 counter 留著（剩唯一 safety net）
- 哲學：trust model + 用 same-tool 抓真 pathology，不限制合理 deliberation 長度

**c. REVIEW field in think grammar**
- `templates.py` grammar 加：`"REVIEW: " line` between INTENT 和 TOOL
- `scaffolding.jinja` 加 `# Think structure` section 解釋 4 欄位語意，特別教 REVIEW = 「看自己卡住沒卡住，卡了就換 tool」
- `character.jinja` 思考方式範例對齊 4 欄位結構

**Smoke**：3 sampling runs、fresh data each
- Run 1: **8/8** 完美
- Run 2: 7-8/8（T2 偶發 fabricate「可口可樂」，但 cascade 行為健康）
- Run 3: **8/8** 完美
- **0 cascade loops, 0 timeouts**（vs 之前偶發 96-iter loop）
- 每 run 19-26 dispatches（穩定，不再 variance 12-96）
- T5 cwd 理解 3/3、T7 cross-turn recall 3/3、T8 skill creation 3/3
- Verbose log 確認 REVIEW 內容是真實 reasoning：「這是第一次回應這個問題」/「看紀錄：」之類

**Verbose 觀察 (root cause confirmation)**：T2 looping 96 次的真實樣貌：
- Phase 1（前 ~20 iters）：query 變化（`drink preference` / `Gura shark drink water` / `favorite` / ...）— deliberation runaway
- Phase 2（中段）：query 開始重複（`Gura likes` ×2、`Gura shark` ×2）
- Phase 3（後 ~70 iters）：完全相同 query「Gura shark」+ 完全相同 think 3 行模板 — 標準 stuck loop
- 結論：B4 grammar 強迫 think 短，**沒有空間寫「我已試 N 次沒結果，換」**

**仍遺留**：
- T2 偶發 fabricate 用戶偏好（character/scaffolding 對「不知道就不瞎掰」anchor 還有點弱）

下個 step 候選（按優先序）：
1. T2 fabrication anchor 強化（小 fix）
2. Subagent / Wake gating / Voice pipeline / Character pack（功能擴張，baseline 已穩）

### 15. Subagent — ephemeral async worker + structured Report  ✅ Merged

Spec calls Subagent「ephemeral, definition inline, dies after run, results re-enter event queue」。Step 15 落地 MVP。

範圍：
- **`SpawnSubagent(task, timeout_s)`** main-only tool — Doll call 後立刻 return「subagent X dispatched」，背景 asyncio.Task 跑 sub-cascade
- **`Report(status, summary, details)`** sub-only tool — subagent 必須 call Report 才算結束（status: ok / incomplete / timeout / error / no_report）
- **`SUB_TOOLS`**：`Shell, NoteMemory, Recall, InvokeSkill, Report` — 沒 Say、沒 SpawnSubagent（不遞迴）、沒 WriteDiary
- **`SubagentResultEvent`** RawEvent — sub-cascade 結束時 fire 進 dispatcher event queue → 變新 turn 的 perception
- **`SubagentRunner`**（新 module `src/dollos/subagent.py`）— asyncio.Task 管理，wall-clock timeout via `asyncio.wait_for`，sub-cascade 內部沿用 same-tool 3-fail counter
- **`subagent_scaffolding.jinja`** — 精簡 worker scaffolding（無 character / 無 [Memory context] / 無 [Recent activity] / 強調必須 call Report）
- `dispatcher.py` `_perceive` 新增 SubagentResultEvent 處理：
  ```
  你派出的 subagent 回來了：
  - task: ...
  - status: ok|timeout|error|no_report
  - summary: ...
  - details: ...
  ```

設計選擇（用戶決定）：
- Result 結構化（不用 free-form Say，必 Report tool）
- Timeout per-spawn（Doll 自估）
- 並行不限

Tests：248 passed（含 6 個 subagent unit test：完成 / timeout / no_report / runtime error / 並行 / SUB_TOOLS grammar exclude Say+SpawnSubagent）。

Smoke：~23/24（3 sampling runs，sampling fluke run 1 T3 hallucinate「我是狗」，無 regression）。SpawnSubagent 未由 T1-T8 觸發 — 邏輯由 unit test 覆蓋；未來 end-to-end smoke 加 T9 case 觸發。

**Bug 紀錄**：實作 agent 第一次跑 pytest **OOM**（51GB RAM），原因是 `test_dispatcher_passes_subagent_runner_into_tool_ctx` 的 capture tool 返回 string → cascade 繼續 → `_FakeAdapter` 重 yield 同樣 chunks → 同 tool 連續 success（counter 不抓） → infinite cascade。修法：tool 改 side-effect capture + return None。揭露 step 14 移除 MAX_CASCADE_DEPTH 後 same-tool counter 對「success-only loop」的盲點 — 但 production 場景小，靠 model 訓練自停（rolling compact 確認可行）。

下個 step 候選：Wake gating / Voice pipeline / Character pack / e2e subagent smoke。

### 19. Mood — Self-First emotional state via big-model think field  ✅ Merged

**動機**：Spec §8 Self-First killer feature。Doll 該有持續的情緒狀態，每 cascade 演化、surface 進下個 perception 影響行為、可被 Recall。

**設計轉折**：原計畫小模型 post-hoc compact 同時出 summary + mood。實作後發現小模型寫 mood 不可靠（meta leak、paragraph 爆炸、思考出聲）— 0.6-1.7B 模型在 cascade 上下文壓力下分不清 emotional snapshot 跟 cascade summary。

**改設計**：mood 由大模型在 `<think>` 區塊內決定（B4-typed grammar 加第 5 欄位 MOOD，介於 REVIEW 和 TOOL 之間）。

範圍：
- Grammar：`think ::= "SEEN: " line "INTENT: " line "REVIEW: " line "MOOD: " line "TOOL: " tool-name "</think>"`
- Scaffolding `# Think structure` 加 MOOD 欄位描述（範例「平淡」/「有點累」/「好奇主人提的新東西」/「鬆了一口氣」）
- Dispatcher：`_current_mood: str` 默認「平靜，剛醒來」；`[Mood]` block 注入 perception（在 `[Now]` 後）；cascade 結束 parse last assistant 的 MOOD line → update + persist
- Persistence：`data/memory/mood/{date}.md` 累積 `## (HH:MM:SS) {mood}` lines；memsearch.index_file 索引 → Doll 可 Recall 過去心情
- 小模型 `compact_cascade` 簡化回 summary-only

**沒做的**（incremental）：discrete emotion categories、PAD 模型、衰減 timer、mood-gated reflex、跨 daemon restart 持久化、character-level mood baseline。

**為什麼大模型寫好過小模型**：
- B4 grammar 強約束 — 不會 paragraph 爆炸
- 大模型語境敏感 — 不寫 meta annotations
- 哲學上：mood 是 Doll **此刻怎麼感受**，不是觀察者事後評
- 整合 deliberation：mood 跟 SEEN/INTENT/REVIEW 一起評估，model 用 mood guide 自己語氣

Smoke：T1-T8 ~7/8。Mood 條目全部單句乾淨：「平靜、溫和。」「平靜但帶點好奇，因為主人剛醒來問問題。」「平靜，幫主人記筆記。」T1 行為「早安呀，今天週日想做點什麼？」展示 time + mood + character 完整整合。

Tests：282 passed。

### 18. Time awareness — [Now] block + HH:MM:SS + time-aware Recall  ✅ Merged

**動機**：DollOS 有檔案系統級時間（`{YYYY-MM-DD}.md` 檔名、`[HH:MM role]` 行 prefix、diary header），但 **Doll 的 perception 無時間感**。Diary、memory、recall 都該有時間軸才合理。

範圍（second precision throughout）：
- **`[Now]` block 注入**：每 cascade first user msg 開頭 `2026-05-10 14:23:05 週六下午`（中文 day-of-week + period-of-day descriptor）
- **`[Recent activity]` 帶時間**：rolling buffer 從 `list[str]` 改 `list[tuple[datetime, str]]`，render 用 HH:MM:SS（跨日加完整 YYYY-MM-DD）
- **Memsearch hits 帶日期**：IV.recall() 給 small-LLM 的 candidates string 加 file-date prefix
- **`Recall` tool 加 since/until**：optional `datetime | None`，filter granularity 是 day（用 `.date()` 比較）；hits 出來帶 `2026-05-08 ...` 前綴
- **`append_transcript`** 行 prefix：`HH:MM` → `HH:MM:SS`
- **`WriteDiary`** header：`HH:MM` → `HH:MM:SS`

**Doll 怎麼用**：從 `[Now]` 拿到當前日期 → 自己算 yesterday/last_week → call `Recall(query, since=ISO, until=ISO)`。Tool 不做 NL date parsing，只接 ISO format。

**沒做的**（incremental）：relative-time render、uptime tracker、time-aware diary cross-day reference、self-initiated time events、timezone、minute/second-level Recall filter（只到 day）。

Smoke：T1-T8 8/8。T7 表現特別好——「你剛才問了我 pwd、data 在哪裡，還有 Qwen3 的 prompt format 怎麼記下來」chronological summary 出來了。

Tests：270 passed（12 new）。

### 17. Doll pack — directory + doll.toml manifest  ✅ Merged

**動機**：`experiments/test_character.jinja` 是當前 character source，單 jinja 整塊內容塞 `{{ character }}` 變數。問題：identity content 跟 mechanism content 混在一起（思考方式 / 工具方式 已過時且 scaffolding 重複）；無結構容納未來 voice / avatar / wake；命名 `experiments/` 暗示 scratch。

按 incremental 原則只做需要的：
- 目錄當 pack：`character_packs/gura/` 純 dir，無 `.doll` 後綴 / 無 archive
- `doll.toml` 單一 manifest，分 `[meta]`（id, name）+ `[identity]`（self / personality / taboos）
- TOML 給結構，每個 value 是 Markdown blob → scaffolding template 控制 layout
- Stale mechanism sections 搬遷時直接砍

**沒做的**（將來需要再加）：pack/templates override、voice / avatar / wake / seed_memory / skills 子目錄、per-character memory namespace、archive 格式、簽章 / store、hot-reload、Markdown validator、`version` / `description` / `author` 欄位。

範圍：
- 新 `dollos.character` 模組 — `PackMeta` / `Identity` / `DollPack` pydantic models（extra=forbid）+ `DollPack.load(pack_dir)`
- `scaffolding.jinja` `{{ character }}` → `{{ identity.self }}` / `{{ identity.personality }}` / `{{ identity.taboos }}`，layout 在 scaffolding，data 在 doll.toml
- `CharacterConfig.profile_path` → `pack`
- `kernel.py` boot：load DollPack → 傳 identity 進 dispatcher
- `dispatcher.py` `character_profile: str` → `identity: Identity`
- 刪 `experiments/test_character.jinja`

Smoke：T1-T8 8/8（identical behavior，純 refactor）。Tests：258（5 個新 pack loader tests）。

### 16. IPC pump — per-connection persistent sink  ✅ Merged

**動機**：step 15 e2e smoke 揭露 IPC 架構不支援 daemon-initiated events。WS handler 每 text_input 開新 sink、turn 結束推 None → handler iterator return → sink 死。Subagent 之後 fire SubagentResultEvent 拿到死 sink → Doll 的 TURN 2 Say 寫到 /dev/null。

**修法**：per-WS-connection 持續 sink + pump task。Handler 改 void signature，IPC server 起 pump 在背景 drain `sink → ws.send` forever；None 變 turn separator（pump skip 繼續），connection close 才 cancel pump。

範圍：
- `ipc/server.py`: `Handler` 改 `(TextInput, sink) → Awaitable[None]`；新 `_pump` async method；`_on_connect` 起 pump_task、傳 sink 給 handler、disconnect cancel pump
- `kernel.py`: `_handle_text_input(msg, sink)` 改 void，`dispatch` 後立刻 return
- `dispatcher.py`: **不動**（None 持續是 turn separator 語意）

Manual smoke `/tmp/smoke_subagent.py` 確認 e2e flow：
- TURN 1: 「分身已經出發去查了，等它回來就告訴你 a~」
- TURN 2: 「分身回來啦～/tmp 目錄一共有 50 個檔案跟資料夾 a~」

T1-T8 smoke 8/8 無 regression。Tests: 250 passed。

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
