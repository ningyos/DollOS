# 工具合併：計畫

**依據**：`docs/research/2026-10-28-v1-tool-use-acceptance.md`（發現 4、5，和「修補後重跑」）、`docs/superpowers/plans/2026-10-28-tool-use-blockers.md`「不在範圍內」
**日期**：2026-10-29
**狀態**：主人 2026-10-29 LGTM；Task 1–3 完成，Task 4 重跑完（2026-10-29），沒全過：3.1 呼叫 ≤ 2 沒達到、1 次答錯，見 `docs/research/2026-10-29-tool-consolidation-rerun.md`。未合併
**分支 / worktree**：`feat/tool-consolidation`，`.worktrees/tool-consolidation/`（從本機 `main` `0380e9f` 開）

---

## 一句話

Doll 的短 Shell 當輪就拿到結果，不用再等一個新回合；把兩次驗收都幾乎沒用到的 `GrepToolOutput`、`SpawnMonitor`/`RemoveMonitor`、`Scratchpad` 從她的工具裡拿掉。她的工具從 18 個（加上 extras）變成 14 個，簡單的事少 2 次呼叫、約 10 秒。

---

## 實際用量（重跑的 21 個 rep，`cascade_log` 的 `tool_call`）

| 工具 | Doll（conversation） | worker | 備註 |
|---|---|---|---|
| Shell | 25 | 29 | |
| Scratchpad | 26 | 0 | 每個任務開頭寫「waiting for result」、結尾清掉，沒看到幫上忙 |
| ReadToolOutput | 15 | 14 | 幾乎每個 Shell 結果都讀一次，輸出通常只有幾行 |
| GrepToolOutput | 1 | 1 | 她直接在 Shell 裡 grep |
| SpawnMonitor / RemoveMonitor | 0 | 0 | 基準也是 0；3.6 長命令她 3/3 選 Shell + 長 timeout |

基準那次（30 rep）的數字一樣：GrepToolOutput 1、SpawnMonitor 0、Scratchpad 35、ReadToolOutput 34。

---

## 這個 plan 的決定

1. **Doll 的短 Shell 當輪回結果**：`timeout_s ≤ 30` 時，Doll 的 `Shell.run` 也走 `run_and_wait`，工具結果直接帶 status、exit code、output_id 和前 10 行（跟 worker 現在一樣的格式，只是不提 workflow）。`timeout_s > 30` 照舊 fire-and-forget，結果以新回合的 `ToolResultArrived` 回來。說明改成講這兩種情況。
   - 等結果的時候主人又說話：訊息照樣進 queue，這輪工具結果回來後她會在下一輪看到（實作時要用測試確認，不是假設）。
   - 30 是 tool-use 驗收裡所有短命令（0.1–0.3 秒）和 3.6 長命令（約 60 秒以上）中間的分界。
2. **拿掉 `GrepToolOutput`**：要看更多就 `ReadToolOutput`（翻頁）或在 Shell 裡 grep。從 `MAIN_TOOLS`、`SUB_TOOLS`、`TOOL_MATRIX`、safe mode 和 external_public 的工具組、executor 的降權判斷裡拿掉；`ToolOutputStore.grep` 沒有其他使用者的話一起刪。`ReadToolOutput` 留著。
3. **拿掉 `SpawnMonitor` / `RemoveMonitor` 和整個 monitor**：工具、`MonitorRunner`、`MonitorFired` / `MonitorExited` 事件、`[Active monitors]` 區塊、kernel 的接線、action_log 那兩行、`workflow.py` 模組說明裡的那段。git 留著，以後真的要再加回來。實作前先查 `mind_state` 或任何狀態檔有沒有存 monitor 的東西；有的話讀舊檔要不會壞。
4. **Doll 和 worker 不再有 `Scratchpad`**：從 `MAIN_TOOLS`、`SUB_TOOLS` 拿掉，Doll prompt 裡的 `[Scratchpad]` 區塊也拿掉。`MindState.scratchpad` 欄位留著（狀態檔有存），`KEEPER_TOOLS`（記憶 keeper）照舊用它，不動。
5. **不在這次做**：`SetFocus`（重跑 5 次）、`OpenLoop`/`CloseLoop`/`MoodTool`/`PursueGoal`/`AdvanceGoal`（驗收題目不會用到，0 次不代表沒用）、`SpawnWorkflow`、`InvokeSkill`。

---

## Tasks

每個 task 一個 subagent，TDD，最後整個測試套件全綠（`uv run pytest`，現在 2120 passed）。開始每個 task 前先跟你說內容，你說好再派。

### Task 0 — worktree 和基準

- `git worktree add .worktrees/tool-consolidation -b feat/tool-consolidation main`，`uv sync`，跑一次測試記基準。

### Task 1 — Doll 的短 Shell 當輪回結果（決定 1）

- `Shell.run`：`ctx.workflow_runner is None or self.timeout_s <= 30` 走 `_run_and_wait`；結果裡「Inside a workflow …」那句只在 worker 時出現，Doll 的版本說「the result is here; no ToolResultArrived event will follow」。
- worker 的 `MAX_AGENT_TIMEOUT_S` 拒絕只對 worker。
- 說明（docstring）改寫：≤ 30 秒的命令結果直接回來，> 30 秒的照舊以新事件回來。
- 測試：Doll ctx + `timeout_s=5` → 結果在工具訊息裡、queue 是空的；`timeout_s=60` → 照舊進 queue；短命令 timeout；等待中 queue 進了 UserSpoke，下一輪看得到。
- 更新 golden（工具 JSON 變了）。

### Task 2 — 拿掉 GrepToolOutput 和 Scratchpad（決定 2、4）

- 照決定 2、4 的清單刪；grep `src/ tests/ templates/ character_packs/` 確認沒有剩下的名字（說明文字、prompt 範本、技能檔都算）。
- 測試：`MAIN_TOOLS` / `SUB_TOOLS` 沒有這兩個；Doll prompt 沒有 `[Scratchpad]`；舊狀態檔（有 scratchpad 內容）讀得進來。
- 更新 golden。

### Task 3 — 拿掉 monitor（決定 3）

- 照決定 3 刪，`tests/test_monitor_runner.py` 跟著刪，其他測試裡跟 monitor 有關的部分拿掉。
- 測試：daemon 開機和關機不碰 monitor；舊狀態檔讀得進來；`MAIN_TOOLS` / `SUB_TOOLS` 沒有這兩個。

### Task 4 — 重跑驗收

- 用 tool-use 驗收的 `live.py` 和夾具，在 branch 上跑 3.1–3.4、3.6 各 3 次，3.5、3.7、3.8、3.9 各 1 次確認沒退步。
- 過關條件：每個情境照原本的 §6 過；3.1（一個 `ls`）呼叫次數 ≤ 2、時間比基準的 22 秒短；3.6 的長命令照舊走新回合回來。
- 結果寫成 `docs/research/2026-10-29-tool-consolidation-rerun.md`。
- 全過 → 給你看結果，你說好再合併回 main（不推）。

---

## 不在範圍內

- 決定 5 列的那些工具。
- 讓她知道 `sandbox/` 就在 cwd（3.4 的 `~` 問題）：短 Shell 當輪回結果之後先看還會不會出現。
- 測試寫進 repo 真正的 `data/traces/` 的問題。
