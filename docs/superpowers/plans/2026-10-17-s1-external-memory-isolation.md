# S1 — 陌生人的筆記不再自動出現在主人的回合

**Spec**：`docs/superpowers/specs/2026-10-17-doll-harness-design.md` §6.5、§9 S1（APPROVED）
**日期**：2026-10-17
**狀態**：待使用者審
**分支 / worktree**：`feat/s1-external-memory-isolation`，`.worktrees/s1-external-memory-isolation/`（從 `main` 開）

---

## 查證後的現況（比 spec 寫的好一點）

- **誰會寫進 `external_public/`**：所有非主人的人（Discord 公開頻道、別人的私訊、MCP 對端），以及主人在公開頻道說的話。`external_dm/` 在程式裡其實是**主人的私訊**（`mind_loop.py:415` `_derive_origin_tier`），是可信的。
- **已經有的保護**：
  - 陌生人的回合只看得到 `external_public/`（fail-closed allowlist）。
  - 主人回合裡出現的 `external_public/` 記憶，已經會加上 `[外部AI·未驗證]` 標記（R-DECISION-5，`mind_prompt.py:270`、`tools.py:89`）。
- **還沒有的保護**：主人回合和 internal 回合仍然會**自動**把 `external_public/` 的記憶注入兩個地方：
  1. `[Memory context]`：`_derive_memory_hits`（`mind_loop.py:1061`）
  2. `[Associative memories]`：`associative_search`（`mind_loop.py:625`）

  標記只是提醒，弱模型照樣可能照做；而且這些筆記根本不該在主人沒問的時候冒出來。

**S1 要做的**：自動注入一律排除 `external_public/`；只有 Doll 主動用 `Recall` 而且明確要求時，才讀得到（照樣帶標記）。

**S1 不做的**：讀了外部記憶之後讓回合降權。這要等 harness 的動態工具綁定（S5 以後），本 plan 只在 spec 裡註明。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。

### Task 0 — 讓測試能跑

- 問題：`uv` 用 Python 3.14 解析依賴，`piper-phonemize==1.3.0` 只有 cp313 的 wheel，連 `pytest --collect-only` 都失敗。
- 改法：新增 `.python-version`，內容為 `3.13`（uv 會自動下載 3.13.15）。`pyproject.toml` 的 `requires-python` 改成 `">=3.13,<3.14"`。
- 驗收：`uv sync` 成功；`uv run pytest -q` 跑完。**記錄目前的基準結果**（幾個通過、幾個失敗）。如果有原本就失敗的測試，先列出來給使用者看，不在 S1 裡修。

### Task 1 — `[Memory context]` 排除外部記憶

- 檔案：`src/dollos/mind/mind_loop.py` 的 `_derive_memory_hits`
- 改法：非 `external_public` 的回合，呼叫 `memsearch.search` 時帶 `exclude_prefixes=[<memory_root>/external_public/]`。`FtsMemory` 已經支援這個參數，而且有目錄邊界檢查（`fts_store.py:242`）。`external_public` 回合維持原本的行為（直接回傳空）。
- 測試（新增到 `tests/test_mind_loop*.py` 或獨立檔案）：
  - internal 回合：`shared/` 和 `external_public/` 各放一條會命中的筆記 → 結果只有 `shared/` 那條。
  - `external_dm`（主人私訊）回合：同上。
  - `external_public/` 的兄弟目錄（例如 `external_public_evil/`）不受影響，確認邊界正確。

### Task 2 — `[Associative memories]` 排除外部記憶

- 檔案：`src/dollos/mind/associative_search.py`、`mind_loop.py:625`
- 改法：`associative_search` 新增 `exclude_prefixes` 參數，原樣傳給 `memsearch.search`。只在有值時才加進呼叫參數，保持其他測試替身的相容性（沿用 `source_prefix` 的作法）。`mind_loop` 在非 `external_public` 的回合傳入 `external_public/`。
- 測試：同 Task 1 的三種情況，換成 associative 路徑。

### Task 3 — `Recall` 預設不讀外部記憶，要明確要求

- 檔案：`src/dollos/tools.py` 的 `Recall`
- 改法：
  - 新增欄位 `include_external: bool = False`，說明文字寫清楚：「包含陌生人和外部 AI 留下的筆記（未驗證）」。
  - 非 `external_public` 回合：`include_external=False` 時帶 `exclude_prefixes=[external_public/]`；`True` 時不排除，命中的外部筆記照樣有 `[外部AI·未驗證]` 標記。
  - `external_public` 回合：維持原本的 allowlist，`include_external` 沒有作用。
  - action log 的摘要加上是否讀了外部記憶，之後的降權機制可以用。
- 測試：
  - 主人回合，預設 → 沒有外部筆記。
  - 主人回合，`include_external=True` → 有外部筆記，而且帶標記。
  - 陌生人回合 → 只有外部筆記（跟現在一樣），不管 `include_external` 怎麼設。

### Task 4 — 更新 spec 和註解

- harness spec：
  - §6.3：principal 表改成跟程式一致——`external_dm` 是**主人的私訊**，非主人的私訊算 `external_public`。工具矩陣的 `external_dm` 欄跟著調整（主人私訊的權限比照 owner，記憶範圍不受限）。
  - §6.5：把「主人回合的檢索沒有任何來源限制」改成實際情況（已有標記、S1 改成排除），並註明降權留到 S5 以後。
- 程式註解：`mind_prompt.py:270`、`:293` 和 `tools.py:89` 一帶關於 R-DECISION-5 的註解，補一句「S1 之後自動注入已排除，這個標記只剩 Recall 明確要求時會出現」。

---

## 驗收

- Task 0 記錄的基準測試加上新測試全部通過（原本就失敗的除外，並且已列給使用者）。
- 手動檢查一次：在暫存的 data 目錄裡放一條 `external_public/` 筆記，用主人身分的文字輸入觸發一個回合（不需要真的 LLM，用測試替身即可），確認 prompt 裡的 `[Memory context]` 和 `[Associative memories]` 都沒有它。

## 不在範圍內

- 讀了外部記憶後的回合降權（harness S5 以後）。
- `consolidation` 和 `evolution` 的讀取路徑：它們讀的是 `transcripts/`、`shared/`、`consolidated/`，不讀 `external_public/`，已在 P1e / 日記 spec 處理過隔離，S1 不動。
- 外部筆記標記的文字（`外部AI` 對 Discord 陌生人不太精確），之後再改。
