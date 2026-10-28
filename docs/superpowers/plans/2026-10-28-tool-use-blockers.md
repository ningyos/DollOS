# 工具驗收擋路的 4 個 bug：修補計畫

**依據**：`docs/research/2026-10-28-v1-tool-use-acceptance.md`（基準 FAIL，main `414b621`）、spec `docs/superpowers/specs/2026-10-28-v1-tool-use-acceptance-design.md`
**日期**：2026-10-28
**狀態**：MERGED 2026-10-28（使用者「合併（不推）」；fast-forward 到 `526160e`，沒推）。重跑結果：擋路的 4 個情境都過，3.4 是 2/3（第 1 次沒過，加跑 2 次都過）
**分支 / worktree**：`fix/tool-use-blockers`，`.worktrees/tool-use-blockers/`（從本機 `main` 開）

---

## 一句話

修完這 4 個 bug 再重跑一次驗收：她加提醒不會洗掉原本的排程、worker 拿得到自己的 Shell 結果、平行被拒不會讓她沒機會開口、讀過外部內容之後下一回合不會把它當成普通筆記。工具合併不在這份。

---

## 查證後的現況（main `1f53845`）

- **排程**：`WriteSchedule`（`tools.py:638`）只有 `entries`，`write_schedule` 整份覆寫 `memory/schedule/<今天>.toml`。今天的排程不在 prompt 裡，也不在工具結果裡（結果只有「Schedule written: N entries」）。所以她加一筆提醒時，不知道原本有什麼。
- **worker 的 Shell**：worker 和 Doll 共用同一個 `ShellRunner`（`workflow.py:326` 傳進 `run_agent`）。`ShellRunner._run` 把結果放進 Doll 的 perception queue（`shell_runner.py:112/132/159`），worker 的迴圈永遠收不到。worker 的 ctx 是 `workflow_runner is None`（`mind_ctx.py` 的 no-nesting guard）。
- **被拒上限**：`_Failures.add`（`runner.py:300`）每個被拒的呼叫各加 1。她一輪平行送 3 個 `NoteMemory`，同一輪就湊滿 `UNBOUND_REJECT_LIMIT = 3`，回合 `failed / unbound_limit` 結束，她看不到拒絕訊息，也沒機會說話。
- **降權**：`ctx.tainted` 每回合開始時歸零（`mind_loop.py:509`）。讀過的網頁還留在同一頁（`Conversation.messages`，`context_builder.py:61`），下一回合沒降權，她就把網頁內容用 `NoteMemory` 寫進 `shared/`，沒有外部來源標記。

---

## 這個 plan 的決定

1. **排程改成「加／刪」，結果列出整天**：`WriteSchedule` 的參數改成 `add: list[entry]`（預設空）和 `remove: list[str]`（要刪的 `HH:MM:SS`，預設空）。它會先讀今天的檔，刪掉 `remove` 裡的時間，再加上 `add`（同一個時間就覆蓋那一筆），最後寫回。工具結果列出改完後今天的**全部**項目，她看得到自己的排程。另外，新的一頁開頭的 snapshot 加一段 `[Today's schedule]`，列出今天還沒到的項目。不保留「整份覆寫」：她要重排的話，可以先刪再加。
2. **worker 的 Shell 直接等結果**：在 worker 的 ctx 裡（`workflow_runner is None`），`Shell.run` 呼叫新的 `ShellRunner.run_and_wait(command, timeout_s)`。它跟 `_run` 共用執行和存輸出的程式，回傳結果的 dict，不放進 queue。工具結果直接帶 status、exit code、前 10 行預覽和 `output_id`。Doll 的 Shell 不變，還是 fire-and-forget。
3. **同一輪的拒絕只算 1 次**：`_Failures.add` 對每個工具名稱，一輪最多加 1。3 個平行的 `NoteMemory` 被拒，算 1 次，她下一輪看得到拒絕訊息，可以說話。真的一直重試，還是 3 輪就停。
4. **降權跟著頁走，降權時的筆記另外放**：
   - `Conversation` 加 `tainted: bool`。回合開始時，`ctx.tainted = conv.tainted`；回合中被降權時，也設 `conv.tainted = True`。開新的一頁（預算滿、prefix 變了等）時自然歸零，因為網頁內容已經不在頁上了。
   - 降權時 `NoteMemory` 可以用，但一律寫進 `memory/external_owner/`（主人讀的外部內容），不是 `shared/`。`TAINTED_TOOLS` 加 `NoteMemory`。
   - `external_owner/` 跟 `external_public/` 一樣，**不會**自動注入主人回合，只有 `Recall(include_external=true)` 才讀得到（讀了就降權）。陌生人回合的 Recall 只看 `external_public/`，所以讀不到 `external_owner/`。
   - 代價：同一頁上讀過網頁之後，到換頁之前都不能用 Shell、排程、PinSelf 等工具。被拒時的訊息改成說明原因：「this conversation has external content in it」。換頁前能不能讓主人手動解除，這份不做，等重跑的結果看實際有多常卡住。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。開始每個 task 前先跟你說內容，你說好再派。

### Task 0 — worktree 和基準

- `git worktree add .worktrees/tool-use-blockers -b fix/tool-use-blockers main`，`uv sync`。
- 跑測試套件，記下基準（預期只有 `tests/voice/test_tts_qwen3.py` 的 3 個 torch 失敗）。

### Task 1 — 排程加／刪（決定 1）

- `WriteSchedule`：`add`、`remove`，讀檔、合併、寫回；`remove` 裡有不存在的時間，結果裡說哪一個沒找到，其他照做。`add` 和 `remove` 都空的話，回錯誤。
- 工具結果：`Schedule now (N entries): 09:26:00 — …; 20:00:00 — …`。寫進 `shared/<今天>.md` 的那段 Plan 照舊，但只寫這次加的項目。
- 工具說明改寫：講加和刪，不講「Replaces any existing schedule」。
- snapshot：`mind_prompt._snapshot_blocks` 加 `[Today's schedule]`，只列還沒到的項目；沒有就不出現這段。
- 測試：原本 2 筆 + `add` 1 筆 → 3 筆；同時間覆蓋；`remove`；不存在的時間；snapshot 有沒有這段。
- 附帶：工具 JSON 變了，`private` 這頁第一次會換頁（cache 重建一次），照設計。

### Task 2 — worker 的 Shell 直接等結果（決定 2）

- `ShellRunner.run_and_wait`：抽出 `_run` 裡執行、timeout、存輸出的部分給兩邊共用；`_run` 只多一步放進 queue。
- `Shell.run`：`ctx.workflow_runner is None` 時走 `run_and_wait`。Shell 的說明加一句：在 workflow 裡結果直接回來。worker 看到的工具說明跟 Doll 是分開的 JSON，所以不影響 Doll 的 cache；實作時查一下是不是真的分開，不是的話換成在結果字串裡說明。
- 測試：worker ctx 的 Shell 回傳裡有輸出，Doll 的 queue 是空的；Doll ctx 照舊放進 queue；timeout。
- `workflow.py` 的模組說明裡關於 Shell 結果的那段跟著改。

### Task 3 — 同一輪的拒絕只算 1 次（決定 3）

- `_Failures.add`：同一次 `add(results)` 裡，同一個工具名稱的 `unbound` 最多加 1。
- 測試：一輪 3 個平行被拒 → 回合沒結束，下一輪模型看得到 3 個拒絕結果；連續 3 輪各被拒 → `unbound_limit`。
- `runner.py` 模組說明裡那一句改成「3 輪」。

### Task 4 — 降權跟著頁走（決定 4）

- `Conversation.tainted`；`mind_loop.py:509` 改成從這回合的 conversation 讀；executor 的 `_maybe_taint` 同時寫回 conversation（要查 `ctx` 怎麼拿到這回合的 conversation，用最直接的方式傳，不加全域狀態）。
- `TAINTED_TOOLS` 加 `NoteMemory`；`NoteMemory.run` 在 `ctx.tainted` 時寫 `external_owner/`。`WriteDiary` 不加（日記會被摘要和自動注入）。
- 自動注入的 `exclude_prefixes` 加 `external_owner/`；`Recall(include_external)` 讀得到它；external_public 回合的 `source_prefix` 本來就只有 `external_public/`，加一個測試確認讀不到。
- 被降權後被拒的訊息加原因。
- 測試：fetch（假的 untrusted MCP 工具）→ 同回合 `NoteMemory` 寫進 `external_owner/`；下一回合還是降權、Shell 被拒；換頁後不降權；主人回合的自動注入沒有 `external_owner/` 的內容；陌生人 Recall 讀不到。
- 更新 harness spec §6.5 的「已知缺口 (a)」和降權那段。

### Task 5 — 重跑驗收

- 合併前，在 branch 上重跑驗收的 3.5、3.7 強制版、3.8、3.9（各 3 次），用上次的 `live.py` 和夾具。3.1–3.4、3.6、3.7 再各跑 1 次，確認沒有退步。
- 結果附在 `docs/research/2026-10-28-v1-tool-use-acceptance.md` 後面，新增一節「修補後重跑」。
- 3.8 這次的過關條件：她在讀網頁那一回合就存進 `external_owner/`、跟主人說存了，而且是真的存了。
- 全過 → 給你看結果，你說好再合併回 main（不推）。

---

## 不在範圍內

- 工具合併（GrepToolOutput、SpawnMonitor、Scratchpad 等的去留）：等重跑之後另寫 plan。
- Doll 自己的 Shell 改成直接等結果（省下約 2 次呼叫、10 秒）：屬於工具合併。
- 3.2 那 1 次中途搶答：先看重跑還會不會出現。
- `NoteToolLesson` 在一般回合被拒。
- 降權的手動解除、排程記錄建立者 principal（mind-loop M1）。
