# Grammar 與 Prefill 注入：本地推理模型的思考框架技術

> 適用對象：Qwen3.6-35B-A3B 等具 `<think>` 區塊的推理模型，部署於 llama.cpp / llama-server。
> 撰寫日期：2026-05-01。所有數字皆來自本檔附錄的可重現實驗。

## TL;DR

1. 對 `<think>` 區塊套用 **GBNF 約束**（GOAL/APPROACH/EDGE 三行）可在 HumanEval / 自製陷阱題上維持 16/17 正確率，think token 壓縮 10–22×、總 token 6–8×、wall-clock 加速約 6×。
2. 加更多欄位（STATE/VERIFY/ALGO 共 5 欄）反而把 pass rate 從 94% 拉到 59%——**框架要對齊模型 RL 訓練分布，不是越多越好**。
3. 把手寫 **failure lesson** 注入 think 區塊，可將特定 bug 出現率從 80% → 0%（merge intervals 的 +1 陷阱），token 開銷僅 ~100。
4. 注入有兩種等效機制：**GBNF 強制生成** vs **prefill 直接嵌入**。後者實測**快 2.6×、completion token 少 2×**，效果完全相同。
5. 注入只對「模型不可能透過訓練學到的內容」有效——通用知識（Python 負數 mod 語義）注入無效。
6. 整套方法的 mental model：**GBNF 是給模型的思考框架，不是 token 壓縮工具**。對 multi-turn agent 場景特別有價值。

---

## 1. 背景

### 1.1 推理模型的 think 浪費

Qwen3、DeepSeek-R1、QwQ 等模型在 `<think>` 區塊花費數千 token 做「探索、復述、自我懷疑」。觀察到大量內容是 **scaffolding 不是 essential computation**：

```
The user wants me to ... let me trace ...
Wait, let's check ... actually it's ...
Hmm, but what about the edge case ...
```

這些內容對 model 內部運算的貢獻往往很低，但每個 token 都吃 forward pass 與 KV cache。

### 1.2 GBNF（GGML BNF）

llama.cpp 的 grammar 格式，作用層在 sampling 階段：每生成一個 token，將不符合 grammar 的 token logit mask 為 −∞。**不改變 model 內部計算，只改變允許輸出的 token 集合**。

關鍵事實：
- Grammar 不影響 transformer forward pass，只影響 sampler。
- 合法 token 之間的相對機率保持不變。
- 強制 token（literal）等同於 model「自己寫過了」，會進入後續步驟的 KV cache 並被 self-attention 處理。

### 1.3 既有 memory recall 範式

LangChain / MemGPT / Generative Agents 等系統皆透過 **prompt 注入**（system / user message）提供 memory。從 model 視角是「外部給的參考」。本文探討另一條路：**透過 token 在 assistant role 的位置讓 memory 變成「自己寫過的」**。

---

## 2. 方法

### 2.1 基線 GBNF（FSM_BASE）

```gbnf
root   ::= think answer
think  ::= "GOAL: " line "APPROACH: " line "EDGE: " line "</think>\n\n"
line   ::= [^\n]+ "\n"
answer ::= [\x09\x0a\x0d\x20-\x7e]+
```

部署設定（關鍵 flag）：
```bash
llama-server -hf unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL \
    --jinja \
    --reasoning-format none \              # 必要：否則 grammar 在 think 內失效
    --chat-template-kwargs '{"enable_thinking": true}' \
    ...
```

**`--reasoning-format none` 是必要條件**。預設的 `deepseek-legacy` 會把 think 區塊包成獨立欄位，grammar 套不到 think 內容；llama.cpp Issue #20345 文件化的破解方式即是改成 `none`。

Grammar 呼叫方式（per-request body field）：
```json
{"model": "...", "messages": [...], "grammar": "<gbnf string>"}
```

### 2.2 注入機制 A：Grammar literal（強制生成）

把 memory 文字寫成 GBNF 的 literal string。Sampler 強制 model 一個一個 token 「吐出」這段文字：

```gbnf
think ::= "GOAL: " line
          "MEMORY: <lesson_text_with_escape>\n"
          "APPLY: " line "</think>\n\n"
```

代價：每個 memory token 仍需一次 forward pass。

### 2.3 注入機制 B：Prefill（直接嵌入）

走 `/completion` raw endpoint：

```python
rendered = POST("/apply-template", {"messages": msgs})["prompt"]
# rendered 結尾是 <|im_start|>assistant\n<think>\n
full_prompt = rendered + f"MEMORY:\n- {lesson}\nAPPLY: "
POST("/completion", {"prompt": full_prompt, "stop": ["<|im_end|>"]})
```

Memory 在 prefill batch 一次處理（與 prompt 評估同階段），**generation 從 APPLY: 才開始**。

兩者對 self-attention 的效果相同（memory token 都在 assistant 位置進入 KV cache）；差別只在 memory 是 generation 還是 prefill 階段建立。

### 2.4 Voice-of-Mind 變體

不寫死 memory 內容，由小模型（Qwen3-0.6B / 1.7B）即時合成：

```
[history] → [small LM] → synthesized recall (~50–100 tokens)
                              ↓
            [large LM, prefill mode]：rendered + "MEMORY: <synth>\nAPPLY: "
```

適用對話歷史壓縮、長 horizon agent loop。

---

## 3. 實驗結果

所有實驗：Qwen3.6-35B-A3B Q4_K_XL，雙 4060 Ti tensor-split，`temperature=0.6, top_p=0.95, top_k=20`。

### E1 — FSM_BASE 在 HumanEval 上的壓縮（n=8）

| | FREE | FSM_BASE | 比率 |
|---|---|---|---|
| pass@1 | 8/8 | 8/8 | 持平 |
| mean think tokens | 1638 | 107 | **15×** |
| mean total tokens | 1764 | 262 | **6.7×** |
| mean wall clock | 26 s | 4.4 s | **6×** |

驗證：think 區塊大量內容是 scaffolding，可被 grammar 移除而不損品質。

### E2 — 更多欄位反而崩盤（n=17 自製陷阱題）

| Mode | pass | mean tokens |
|---|---|---|
| FREE | 16/17 (94%) | 4374 |
| FSM_BASE (3 欄) | 16/17 (94%) | 424 |
| FSM_PLAN (5 欄: GOAL/STATE/ALGO/EDGE/VERIFY) | **10/17 (59%)** | 313 |

具體失敗模式：
- **Sudoku**：VERIFY 欄位誘使 model 加防禦性 `cell.isdigit()` 檢查，但題目 cell 是 int → 全部炸掉。
- **Merge intervals**：VERIFY 欄位寫對了「[1,2] [3,4] 不應合併」，code 卻仍用 `last_end + 1`。**VERIFY 變儀式**。

結論：grammar 只能保證 model **寫出**特定字串，無法保證 model **使用**它。欄位太多會稀釋每個欄位的決策密度。

### E3 — 自我質疑欄位的可行設計（n=17，3 種變體）

| 設計 | pass | 機制 |
|---|---|---|
| `CLAIM/CHALLENGE/UPDATED` | 16/17 | CHALLENGE 對齊 RL/debate 訓練分布，捕捉題目陷阱 |
| `PLAN/TEST_INPUT/TRACE/VERDICT` | 16/17 | TRACE 強制在 token stream 上 simulate 輸入（**真實自我驗證**） |
| `GOAL/ASSUMING/IF_WRONG` | 14/17 | IF_WRONG 被 model 重新解讀成 edge case 列舉 → 失效 |

設計原則：**機制化欄位（TRACE）優於敘事化欄位（DOUBT/VERIFY）**。欄位用語必須對應 model 訓練分布中存在的對抗/驗證模式。

### E4 — Voice-of-Mind：小模型 recall 合成

3-turn LRU cache 設計對話：

| | BASELINE（每輪 full history） | VoM（每輪 0.6B 合成 RECALL） |
|---|---|---|
| Class 名穩定性 | LRUCache → LRUCacheWithTTL（**漂移**） | LRUCache 三輪一致 |
| Large prompt tokens | 1706 | **108** (16×) |
| Total tokens (含 small) | 3469 | **3015** (-13%) |

意外發現：full-history baseline 反而 decision drift（model 自己改 class 名），VoM 的 RECALL 把 class name 釘在思考前緣，更穩定。

### E5 — 通用知識注入無效（n=15，3 trials × 2 problems × INJECT/BASE）

| 問題 | LESSON 內容 | FSM_BASE | FSM_INJECT |
|---|---|---|---|
| Python divmod 負數 | Python `//` 向 −∞ 取整、`%` 跟 b 同號 | 4.33/7 | 4.00/7 |
| find_peak 邊界 | 邊界視單側鄰居即可 | 6/6 | 6/6 |

Qwen3.6-35B 已透過訓練熟悉這些，注入冗餘。**證明：對 SOTA 模型，注入它已知的事實沒效**。

### E6 — Failure lesson 注入（n=15，5 trials × 3 modes）

「歧義版」merge intervals prompt 故意誘發 +1 bug。Lesson：「閉區間用 `start <= last_end` 不要 +1，[1,2] [3,4] 不能合併」。

| Mode | pass (40 tests) | +1 bug rate | mean tokens |
|---|---|---|---|
| FSM_BASE | 32/40 (80%) | **80%** (4/5 trials) | 240 |
| FSM_INJECT_DECOY (無關 lesson) | 32/40 (80%) | **80%** (4/5 trials) | 305 |
| FSM_INJECT_REAL (對症 lesson) | **40/40 (100%)** | **0%** (0/5 trials) | 346 |

Decoy（注入無關的「edge case 提醒」）證明**內容必須對症，不是「加東西就會更謹慎」**。Token 開銷 ~106 換來 +25% pass rate。

### E7 — Grammar vs Prefill 同效率對比（n=10，merge intervals 陷阱）

| Mode | pass | +1 bug | mean comp tokens | mean elapsed |
|---|---|---|---|---|
| grammar | 40/40 | 0/5 | 288 | 5.86 s |
| **prefill** | **40/40** | **0/5** | **142** | **2.23 s** |

效果一模一樣（兩種都 100% 消滅 bug），prefill **2.6× 快、completion tokens 少 2×**。差距完全來自 lesson literal：grammar mode 強迫 model 一個一個生成這段文字，prefill 直接放在 prompt 裡。

---

## 4. 設計原則

從 E1–E7 萃取的原則：

### 4.1 框架要對齊模型訓練分布
3 欄位 GOAL/APPROACH/EDGE 工作；5 欄位 GOAL/STATE/ALGO/EDGE/VERIFY 崩盤。Qwen 內部對 `<think>...</think>` 與 RL/debate 用語熟悉；硬塞 5 欄位變 OOD。
**換 model 全套 grammar 都要重調**（Qwen ≠ Gemma，Gemma 用 `<|channel>thought\n...<channel|>`）。

### 4.2 欄位要機制化，不能敘事化
- 機制化（會被使用）：`TEST_INPUT`、`TRACE`、`CHALLENGE`、`UPDATED`
- 敘事化（變儀式）：`VERIFY`、`DOUBT`、`IF_WRONG`、`SAFETY_CHECK`

判別法：**這個欄位的內容能否被 model 在後續 token 上具體引用？** 能 → 機制化；只能寫一句宣告 → 敘事化。

### 4.3 注入要對症
- ✅ Contextual / temporal：本 session 決策、user 偏好、近期事實
- ✅ Private / domain-specific：你的 codebase convention、internal API
- ✅ Failure lessons：過往錯誤模式 + 修正方向
- ❌ 通用知識：模型訓練資料已涵蓋（公式、語言語義、知名演算法）

### 4.4 機制選擇
- **預設用 prefill**：相同效果、更快、更省 token、無 escape 麻煩
- **要強制 schema 時用 grammar**：例如要 parse model 輸出的固定欄位做後處理
- **不要 grammar + prefill 混用**：複雜度爆炸，沒有額外收益

### 4.5 Per-request 切換是必要能力
- llama-server 的 `grammar` 與 `/completion` raw prompt 都是 per-request
- 不重啟即可換 mode、換 lesson、開關 think 約束
- 多 role agent（planner / executor / critic）可在同一 server / 同一 KV cache 切換

---

## 5. 參考實作

`lesson_injector.py`（130 行純 stdlib）：

```python
inj = LessonInjector(
    server_url="http://127.0.0.1:8001",
    model="unsloth/Qwen3.6",
    store_path="lessons.json",
)

inj.store.add(
    id="closed_intervals_no_plus1",
    triggers=["closed interval", "merge_intervals"],
    text="For closed integer intervals use `start <= last_end` (no +1).",
)

# Auto-match by keyword + prefill mode (default-recommended)
r = inj.complete("Write merge_intervals ...", mode="prefill")
```

核心元件：
- `Lesson` dataclass: `id`, `triggers`, `text`, `notes`
- `LessonStore`: JSON file 持久化、keyword match
- `LessonInjector`:
  - `complete(..., mode="grammar")` — GBNF literal injection
  - `complete(..., mode="prefill")` — `/completion` raw endpoint with embedded literal
  - `record_outcome(...)` — 事後標記 pass/fail，未來可用於 lesson 自動修剪

---

## 6. 限制與 open questions

### 已知限制
1. **Lesson curation 仍人工**：當前 keyword match 對 lesson 量 <100 夠用；超過需要 embedding retrieval。
2. **Multi-turn 注入未測**：目前所有實驗皆單輪。Multi-turn agent loop 的 lesson 注入時機選擇（每輪都注？還是只在偵測相關時注？）尚未驗證。
3. **未測小模型**：Qwen3.6-35B-A3B 是中大型 model；小於 7B 的 model 是否同樣受益於 lesson injection 未知（E5 暗示反而可能更需要，因為小 model 「不知道」更多事）。
4. **Decoy effect 不對稱**：本文 E6 顯示 decoy 不負面，但若 decoy 數量大或內容互衝，可能稀釋 real lesson 效果。

### Open questions
1. **Lesson 自動學習**：能否讓 agent 在失敗後自動寫出一條 lesson 入庫？需要一個 lesson generator（或讓 35B 自己反思）。
2. **Lesson 衝突解決**：兩條 lesson 對同一個任務給相反建議時的處理。
3. **與 finetuning 的關係**：grammar injection 是否能視為「廉價的 inference-time finetuning」？某些可被 prompt 替代的 finetune 工作能否改用 lesson 注入達成？
4. **Long-horizon agent 的 lesson decay**：lesson 太多後 token 開銷上升，需要 retention policy（recency / utility / importance）。
5. **與 preserve_thinking 的互動**：Qwen3.6 的 `preserve_thinking=true` 會讓注入的 memory 跨輪保留。是否導致 lesson 累積失控？

---

## Appendix A — 啟動 Server

```bash
./llama.cpp/llama-server \
    -hf unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL \
    --alias "unsloth/Qwen3.6" \
    --jinja \
    --reasoning-format none \
    --chat-template-kwargs '{"enable_thinking": true}' \
    --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
    --ctx-size 131072 --fit on \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --flash-attn on --cont-batching \
    --parallel 2 \
    -ngl 99 --tensor-split 1,1 \
    --batch-size 2048 --ubatch-size 512 \
    --threads 8 \
    --keep -1 \
    --port 8001 --host 0.0.0.0
```

注意：**`--reasoning-format none` 必要**，否則 grammar 不會套用到 think 區塊。

## Appendix B — 重現實驗

| 實驗 | 腳本 |
|---|---|
| E1 | `scot_smoke.py --n 8` |
| E2 | `/tmp/quality_eval.py`（FSM_BASE vs FSM_PLAN）|
| E3 | `/tmp/self_q_eval.py`（CLAIM/TRACE/ASSUME 三變體）|
| E4 | `voice_of_mind.py`（需另起 0.6B server on :8002）|
| E5 | `/tmp/inject_eval.py`（divmod / find_peak）|
| E6 | `/tmp/failure_lesson.py`（merge +1 trap）|
| E7 | `/tmp/bench_modes.py`（grammar vs prefill）|

## Appendix C — 關鍵 chat template 行為

Qwen3 jinja template 預設行為：
1. 開啟 thinking 時，assistant turn 結尾自動加 `<think>\n`
2. 渲染下一輪時，會**剝除前一輪的 `<think>...</think>`**（只留 final answer）

實作含意：
- Grammar 從第一個 generated token 開始套用，所以 grammar 第一個規則必須是 `<think>` **內部**內容（如 `"GOAL: "`），不要包含 `<think>\n` 前綴。
- 跨輪 memory 預設不會持續；需 `preserve_thinking=true` 才保留。
- Lesson injection 為每輪重新注入，與此預設行為相容。

## Appendix D — GBNF 注意事項

llama.cpp 的 GBNF 對 byte-level UTF-8 範圍支援有限，常見的踩坑：

- ❌ `[\xC2-\xDF]` 多 byte UTF-8 byte ranges → 解析失敗
- ❌ 規則內混合 `#` 註解 → 部分版本解析失敗
- ✅ ASCII printable: `[\x09\x0a\x0d\x20-\x7e]+`
- ✅ 否定 char class: `[^\n]+`

**Grammar 解析失敗時 llama-server 會靜默忽略 grammar，不回錯誤給 client**——必須檢查 `server.log` 確認 `failed to parse grammar` 字樣不存在。

---

*— 結束 —*
