# Think A/B on Qwen3.8：格式化 think vs 自由 think + 上限

**狀態**：DRAFT（待使用者審）
**日期**：2026-10-17
**關聯**：`2026-07-07-latency-reflex-think-design.md`（延遲壓縮；本實驗取代其 Part 2 的前置量測）、`scripts/persona_stability_smoke.py`（隔離實例 harness 的範本）、`src/dollos/telemetry/turn_latency.py`（turn 級延遲遙測，已實作）

---

## §0 要回答的問題

大模型換成 **Qwen3.8-Flash-Next**（125B MoE、約 6B active，透過 llama.cpp 提供）之後：

- **Q0 基準**：現有管線（`qwen3-thinking` 樣板 + `build_voice_first_grammar`）在新模型上能不能正常跑？延遲和 think 長度是多少？
- **Q1 主問題**：把 think 從五行格式（SEEN / INTENT / TOOL / REVIEW / MOOD）改成**自由文字 + daemon 強制的 token 上限**，行為品質會不會變差？延遲會不會變好？
- **Q2 上限值**：自由 think 自然會想多長？上限設多少，品質才不會掉？這組數字之後會直接變成 effort 的 low / mid / high。

**不在本實驗回答**：要不要整個拿掉 grammar、改用原生 tool calling（Arm C，另開實驗）；Doll 自選 effort（下一個實驗，會用到本實驗的上限值）。

---

## §1 前置條件

1. Qwen3.8 的 llama-server 位址（使用者稍後提供）。記錄 server 版本、量化、啟動參數（特別是 MTP / speculative 有沒有開）。
2. 修好開發環境：`uv` 目前用 Python 3.14 解析依賴失敗（`piper-phonemize` 只有 cp313 wheel），要把 Python 固定在 3.13，讓測試能跑。
3. **基準閘門**：Arm A 必須先在新模型上正常跑完一輪（grammar probe 通過、樣板相容、沒有格式錯誤）。如果 A 本身就跑不起來（例如 chat template 不相容），實驗停止、回報使用者，先處理相容性。

---

## §2 實驗組（Arms）

所有組別使用**同一個模型、同一份 character pack、同一組 sampling 參數**（沿用正式設定：temp 0.6、top-p 0.95、top-k 20），只改 think 的處理方式。

| Arm | think 格式 | think 上限 | 用途 |
|---|---|---|---|
| **A（對照）** | 現行五行格式，每行有上限（`_THINK_LINE_CAP` / `_REVIEW_LINE_CAP`） | 由 grammar 決定（最多約 376 字） | 基準 |
| **B-free** | 自由文字 | 只有安全上限 4096 token | 量測自然的 think 長度分佈；品質的上界 |
| **B-512** | 自由文字 | 512 token | 未來的「mid」候選 |
| **B-128** | 自由文字 | 128 token | 未來的「low」候選 |

說話和工具呼叫的輸出格式（`segments ::= (speak | tool-call)*`）**四組都保留不變**，這樣差異只來自 think。

### §2.1 自由 think 在 B 組裡怎麼運作

- **grammar**：`think ::= [^<]* "</think>\n\n"`，後面接原本的 `segments`。代價是 think 裡不能出現 `<`，可以接受。
- **上限**：daemon 在串流時計算 think 的 token 數。到達上限 → 中斷這次生成，把「prompt + 已生成的 think + `\n</think>\n\n`」當成新的 prompt，用只有 `segments` 的 grammar 接續生成。遙測記錄 `think_capped = true`。
- **token 計數**：用 llama.cpp 的 `/tokenize` 精算，或用字數近似；實作時決定，但誤差要在 10% 以內。注意 MTP 可能一個串流 chunk 帶多個 token，不能只數 chunk。
- **REVIEW 和 MOOD 的去向**：B 組沒有 REVIEW 行，所以 `[Recent self-review]` 區塊**明確不渲染**（不是留空）。沒有 MOOD 排練行，mood 只靠 `Mood` 工具更新。這兩個影響都在 §4 量測。
- `cascade_log` 和 trace 改存原始 think 文字。

---

## §3 情境集

12 個腳本化情境，透過 WS IPC 對**隔離的 DollOS 實例**執行（沿用 `persona_stability_smoke.py` 的作法：IPC 用系統分配的 port、`data/` 用全新暫存目錄），並預先放入一份固定的記憶 fixture。內部事件類情境直接在 process 內放入 perception。

| # | 情境 | 輸入 | 預期行為（機械檢查） |
|---|---|---|---|
| S1 | 寒暄 | 「你好」 | 有回話；不洩漏格式 |
| S2 | 分享近況 | 「我最近在用 blender 做一個模型，正在卡頭髮的部分」 | 有回話；在角色裡 |
| S3 | Shell 任務 | 「幫我跑 `echo hello && ls /tmp \| head -20`，告訴我有沒有 dollos 相關的東西」 | 呼叫 `Shell`；結果回來後回報內容 |
| S4 | 跨回合回想 | 接在 S2 之後：「我剛剛跟你說我在做什麼？」 | 答出 blender／頭髮 |
| S5 | 記住偏好 | 「記下這個：我習慣用 25°C 冷氣」 | 呼叫 `NoteMemory`，內容含 25°C |
| S6 | 打斷 | S3 執行中送出「等等，先停下。」 | 停止或確認停止；不繼續回報 |
| S7 | 長期記憶 | 「我上週說我想去哪裡？」（答案在 fixture 裡） | 使用記憶（`[Memory context]` 或 `Recall`）答對 |
| S8 | 情緒事件 | 「我今天被裁員了」 | 呼叫 `Mood` 更新；回應不像客服 |
| S9 | 人格探測 | `persona_stability_smoke.py` 的 probe prompts | 不違反 pack 的 `[enforcement]` 規則 |
| S10 | 多步任務 | 需要兩個以上工具步驟的任務（例：建立一個 skill） | 完成；沒有 cascade exceeded |
| S11 | Idle 醒來 | 直接放入 `Awoke` perception，沒有使用者輸入 | 可以安靜或做一件小事；**記錄 think 長度**（舊模型在這裡空轉了 730 token） |
| S12 | 緊急提醒 | 放入電池 critical 的 system pulse perception | 必須開口提醒 |

情境腳本和 fixture 一起放進 repo，之後換模型或改 prompt 時可以重跑。

---

## §4 量測與評分

### §4.1 延遲（來自 `turn_latency` 遙測）

每回合記錄：`first_speak_ms`（開口前延遲）、`total_ms`、`ttft_ms`、think token 數、說話 token 數、pass 數、`think_capped`。依組別和情境類型報告 p50 / p90 / max，並另外列出 decode tok/s（確認 grammar 有沒有讓 MTP 失效）。

### §4.2 機械檢查（自動，pass / fail）

每次執行逐項檢查：
- §3 表格中該情境的預期行為
- 格式洩漏：說話內容裡出現 `<think>`、`SEEN:`、工具呼叫語法
- 幻覺工具名稱、JSON 錯誤、cascade exceeded、ERROR log
- 人格硬規則：`check_persona_violations` 命中就判 fail

每組的分數 = 通過的檢查項目 / 全部檢查項目。

### §4.3 品質評審（盲測）

- **LLM 評審，成對比較**：同一情境、同一 seed，把 A 和某個 B 組的完整回應並排給評審模型，左右順序隨機，不透露組別。評審在四個面向上選「左較好／右較好／差不多」：**角色一致、回應恰當、自然（不像助理或客服）、簡潔**。報告 B 對 A 的勝率（平手各算半勝）。
- **評審模型**（使用者 2026-10-17 決定）：外部強模型 **Claude Opus 5.5**（`claude-opus-5-5`），不用 Qwen3.8 自己評自己。API key 從環境變數 `ANTHROPIC_API_KEY` 讀，只給評審腳本 `scripts/think_ab_judge.py` 使用，不進 daemon 設定。評審 prompt、模型 id 和每次判決都存進結果目錄，方便重現。
- **使用者抽查**：從成對比較中隨機抽 20 組給使用者盲評。**使用者的判斷優先**：如果和 LLM 評審明顯不一致，以使用者為準，並回頭檢查評審 prompt。

### §4.4 副作用檢查

- **mood 更新率**：S8 中 `Mood` 被呼叫的比例，B 組和 A 組比較（B 組少了 MOOD 排練行）。
- **think 長度分佈**：B-free 的 p50 / p90 / max，依情境類型分開列。這是選上限值的主要依據。

---

## §5 執行次數

- **每個情境 × 每組 = 5 個 seed**，四組用**同一組 seed**（成對比較，降低抽樣變異）。
- 為什麼是 5：之前的 smoke 測試出現過「7/8 其實是抽樣運氣、固定 seed 重跑只有 3/8」。單次結果不可信；5 個 seed × 12 個情境 = 每組 60 次執行，足以看出大的差異。
- 總量：12 情境 × 5 seed × 4 組 = 240 次執行（多回合情境約 400 個回合）。以每回合 5–10 秒估計，約 1 小時。
- **加跑規則**：如果某個決策指標落在門檻 ±5 個百分點以內，該組再加 5 個 seed。

---

## §6 決策規則（事先定好，跑完不改）

1. **A 跑不起來** → 停止，回報相容性問題。
2. **採用自由 think**，條件是 B-free 同時滿足：
   - 機械檢查通過率 ≥ A − 5 個百分點（不劣於 A）
   - 人格硬規則 fail 次數不多於 A
   - LLM 評審勝率 ≥ 45%，而且使用者抽查不認為 B 比較差
3. **選上限值**：在不劣於 B-free（同樣用規則 2 的三個條件比較）的組別中，取上限最小的一組，作為未來的「mid」。B-128 的結果作為「low」的參考：它可以在品質上稍差，但不能出現格式洩漏、工具錯誤這類機械失敗。
4. **B-free 明顯比 A 好**（勝率 ≥ 60%）→ 建議接著做 Arm C（完全拿掉 grammar，改用原生 tool calling），另寫 spec。
5. **B 不如 A** → 保留格式化 think，只用 A 的數據調整延遲（例如縮短每行上限）。

---

## §7 需要的程式改動

全部用明確的設定開關，沒有自動切換：

- `config.py` 的 `[llm]`：新增 `think_mode = "structured" | "free"`（預設 `structured`，行為和現在相同）與 `think_cap_tokens: int | None`。
- `llm/templates.py`：新增自由 think 的 grammar 建構器。
- `mind/mind_loop.py` 的 `_stream_one_pass`：計算 think token、到上限時中斷並接續生成。
- `mind_prompt.py`：free 模式下不渲染 `[Recent self-review]`。
- `cascade_log` / `trace`：free 模式存原始 think。
- `turn_latency` 遙測：新增 `think_mode`、`think_tokens`、`think_capped`、`seed`、`arm` 欄位。
- `scripts/think_ab.py`（常駐工具，不是用完即刪的 smoke）：執行情境、收集結果到 `runs/think_ab/<日期>/`；`scripts/think_ab_judge.py` 呼叫 Claude 做成對盲評，並產生給使用者抽查的 20 組盲評表；`scripts/think_ab_report.py` 產生摘要表。
- 結果寫成 `docs/research/2026-10-xx-think-ab-results.md`。

---

## §8 待決問題

1. ~~**Q1 評審模型**~~：已決定（2026-10-17），外部強模型 Claude 做成對盲評，使用者抽查 20 組，見 §4.3。
2. **Q2 seed 的確定性**：MTP / speculative decoding 開啟時，同一個 seed 不一定能產生一樣的輸出。執行前先用同一個 seed 跑兩次確認；不確定時，成對比較仍然有效，只是變異會比較大。
3. **Q3 情境集是否足夠**：12 個情境偏重對話和工具。如果使用者最在意的是別的行為（例如主動性、沉默的時機），要補情境。
