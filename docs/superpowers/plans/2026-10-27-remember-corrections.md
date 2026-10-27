# 記住糾正：實作計畫

**Spec**：`docs/superpowers/specs/2026-10-27-remember-corrections-design.md`（APPROVED，使用者 2026-10-27「spec LGTM」；開放問題 Q1–Q3 照 spec 的建議：不限每回合改幾條、兩區不另切字數上限、每天整理只看當天）
**日期**：2026-10-27
**狀態**：APPROVED（使用者 2026-10-27「plan 核可」）
**分支 / worktree**：`feat/remember-corrections`，`.worktrees/remember-corrections/`（從 `main` `8fc1ab4` 開）

---

## 一句話

先修私訊原話沒進 transcript 的 bug；再讓主人在場的對話回合能用 `PinSelf` 寫 user／relationship 區；最後加一個每天跑一次的 profile keeper，補上她漏記的糾正、改掉矛盾的舊條目，每筆都要對得上主人當天的原話。

---

## 查證後的現況（main `8fc1ab4`）

- **私訊原話**：`mind_loop.py:437` 寫主人那一行時讀 `p.data.get("text")`；Discord bridge 的 payload 是 `content`（`discord_bridge/client.py:147`），kernel 原封不動放進 `ChannelMessage.data`（`kernel.py:878`）。所以私訊的主人原話從沒寫進 transcript。`tests/test_external_memory_isolation.py:194` 用 `text`，蓋掉了這個 bug。
- **PinSelf 綁定**：只在反思回合（`turn_policy.py` `_REFLECTION_EXTRAS`，`Extra("PinSelf", when="self_profile")`）。`TOOL_MATRIX["PinSelf"]` 是 owner、owner_dm、internal。`Extra.when` 只有 `self_profile`、`evolution` 兩種條件（`turn_policy.py:47`、`_holds`）。
- **PinSelf 的 replace／remove 會跨區找**：`self_profile._find` 在三區裡找 target（`self_profile.py:118`），所以只檢查 `section` 參數擋不住「在 user 區的呼叫改到 self 區的條目」。限制必須檢查**找到的條目在哪一區**。
- **PinSelf 的說明**寫著「reflection turns only」，要改；它本來就在 internal tier 的 offered 清單裡（`self_profile.enabled` 預設 True，`config.py:285`），所以不多一個工具，只是說明變了。
- **transcript 格式**：`- HH:MM:SS Owner: <text>`／`- HH:MM:SS Me: <text>`（`memory_writer.append_transcript`），action log 行另外標記（`is_action_log_line`）。
- **睡眠整理**：`ConsolidationTrigger`（`kernel.py:559`）閒置一段時間、每天最多一次，呼叫 `run_consolidation`（`mind/consolidation.py`），用 `run_agent(kind="keeper")` 跑。
- **基準**：main 上 2080 passed、7 skipped、3 failed（`tests/voice/test_tts_qwen3.py`，沒有 torch）；`tests/voice/bridge/test_mic.py`、`test_speaker.py` 因為缺 PortAudio 要 `--ignore`。

---

## 這個 plan 的決定

1. **對話回合的 PinSelf 只在主人在場時綁**：`Extra` 加一個條件 `"owner_present"`（principal 是 `owner` 或 `owner_dm`），`conversation` 的 extras 加 `Extra("PinSelf", when=("self_profile", "owner_present"))`——兩個條件都要成立。internal 的對話回合（工具結果、排程）不綁。
2. **哪一區能寫，由回合決定，存在 ctx 上**：`MindCtx.pin_sections: frozenset[str]`，MindLoop 每回合設定——反思回合三區都可以，其他回合只有 `user`、`relationship`。`PinSelf.run` 檢查兩件事：`section` 在允許的區裡；`replace`／`remove` 時，target **找到的那一條**也在允許的區裡。不在就拒絕，回清楚的訊息（「the self section is only revised during reflection」）。不靠模型自律。
3. **keeper 用自己的工具類別**：`EditOwnerProfile`（只給 profile keeper）：`section: Literal["user","relationship"]`、`op`、`target`、`text`、`quote`（必填）。它跟 PinSelf 呼叫同一個 `self_profile.apply`。分開的理由：keeper 的 schema 多一個 `quote`，而 Doll 看到的 PinSelf schema 不該有這個欄位；keeper 的工具 JSON 不在 Doll 的 offered 清單裡，不影響她的 cache。
4. **出處檢查是純字串比對**：`quote` 去掉頭尾空白、把連續空白壓成一個後，必須是當天 transcript 某一行 `Owner:` 之後文字的子字串（同樣正規化）；至少 4 個字元（太短的引用什麼都對得上）。`Me:` 的行和 action log 行不算。對不上就拒絕這筆，回錯誤給 keeper，不寫入。
5. **keeper 接在 consolidation 後面**：同一個 `ConsolidationTrigger`，`run_consolidation` 成功之後跑 `run_profile_keeper(target_date=…)`。consolidation 失敗或那天沒有 transcript → keeper 也不跑，log 一行。不另外排程、不重試。設定 `[profile_keeper].enabled`（預設 True）、`max_tokens`、`think_budget_tokens`、`transcript_tail_chars`，跟 consolidation 的設定形狀一樣。
6. **紀錄**：keeper 的改動照 PinSelf 的方式寫進 `self_history.jsonl`，多兩個欄位 `by="profile_keeper"`、`quote`。`self_profile.apply` 加一個 keyword 參數把這兩個欄位帶進 `_log_pin`；不傳時紀錄跟今天一樣（golden／舊測試不變）。
7. **不 fallback**：私訊讀 `content`、本機讀 `text`，明確分開；不寫「text 沒有就讀 content」。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。開始每個 task 前先跟你說內容，你說好再派。

### Task 0 — worktree 和基準

- `git worktree add .worktrees/remember-corrections -b feat/remember-corrections main`，`uv sync`。
- 跑一次測試套件，記下基準（預期 2080 passed、3 個 torch 失敗）。不一樣就列給你看，不在這裡修。

### Task 1 — 私訊原話寫進 transcript（spec §3）

- `mind_loop.py` 寫主人那一行：`UserSpoke` 讀 `data["text"]`，`ChannelMessage` 讀 `data["content"]`。
- 測試：
  - 用 bridge 真正的 payload 形狀（從 `discord_bridge/client.py` 的 dict 照抄欄位）送一則主人私訊 → transcript 有 `Owner:` 那一行，`memsearch` 搜得到。
  - 主人在公開頻道的話照樣寫（今天的行為：主人自己的話），陌生人的話照樣不寫。
  - 把 `test_external_memory_isolation.py:194` 那個用 `text` 的 fixture 改成 `content`，說明理由（它蓋掉了 bug）。

### Task 2 — 主人在場的對話回合綁 PinSelf，限 user／relationship（spec §2.1，決定 1、2）

- `turn_policy.py`：`Condition` 加 `"owner_present"`；`Extra.when` 可以是一個或多個條件（全部成立才綁）；`_holds` 拿得到 principal。`conversation` 的 extras 加 PinSelf。
- `MindCtx.pin_sections`，MindLoop 在設定 `ctx.principal` 的同一處設（反思 → 三區；其他 → user、relationship）。agent 的 ctx 預設空集合（agent 本來就沒有 PinSelf）。
- `PinSelf.run`：檢查 section 和 target 所在的區（決定 2）。`self_profile` 需要一個「target 在哪一區」的查詢函式（重用 `_find`）。
- 測試（`tests/test_harness_turn_policy.py` 的字面集合）：
  - 綁 PinSelf：owner 的 conversation、owner_dm 的 conversation、反思（照舊）。
  - 不綁：internal 的 conversation、external_public 任何回合、diary／agenda／pulse／safe_mode、worker／keeper、讀過外部內容之後（降權，Task 6 的機制，不用改）。
  - `self_profile.enabled = false` 時哪裡都不綁。
  - 對話回合 `PinSelf(section="self")` 被拒；`PinSelf(section="user", op="replace", target=<self 區的 id>)` 被拒、檔案不變；`user` 區的 add／replace／remove 成功。反思回合三區照舊。
- Offered 清單不變：`tests/test_golden_offered_tools.py` 這個 task 不能變。

### Task 3 — 改 PinSelf 的說明（spec §2.1「說明怎麼改」）

- 拿掉「reflection turns only」；保留原本關於「你自己」的段落；加一段：主人糾正你怎麼對待他、或糾正一件關於他的長期事實時，當下就用 replace（有舊條目）或 add 寫進 user／relationship；寫成平實的事實，不寫「我被糾正了」；一次性的事用 NoteMemory 或 WriteSchedule；self 區只在反思時改。英文，不放引號範例句（基礎人設 spec：範例句會被逐字照抄）。
- 重錄 golden：`DOLLOS_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_offered_tools.py tests/test_golden_request_sequence.py`。檢查 diff **只有** PinSelf 的 description 變了（每個 tier 的 offered JSON 和 request 序列裡的那一段）；其他任何一個 byte 變了就停下來回報。
- 這是工具 JSON 唯一會變的一次：Strata 上每段對話的 cache 前綴會重算一次。

### Task 4 — profile keeper（spec §2.2，決定 3–6）

- 新增 `src/dollos/mind/profile_keeper.py`：
  - `run_profile_keeper(*, target_date, model, memory_root, transcripts_root, …)`：讀當天 transcript（去掉 action log 行，取最後 `transcript_tail_chars`）、self_profile 的 user／relationship 兩區（含 id），組任務說明，`run_agent(kind="keeper", tools=[EditOwnerProfile, Report])`。
  - 任務說明（英文）：只處理主人當天**糾正或更新**的長期事實和他希望被怎麼對待；沒有就什麼都不做直接 Report；每筆改動附 `quote`；寫成平實的事實；不寫一次性的事；滿了先刪過時的再加；不碰 self 區（工具也碰不到）。
  - `EditOwnerProfile`：決定 3、4。出處檢查讀的是同一份當天 transcript（不是模型給的文字）。
- `self_profile.apply` 加 `history_extra: dict | None = None`（決定 6）。
- `ConsolidationTrigger`：consolidation 成功後跑 keeper（決定 5）；keeper 的例外記 log，不影響 consolidation 的結果。
- 設定：`config.py` 加 `ProfileKeeperConfig`，`config.example.toml` 加註解過的區塊。
- 測試（fake model）：
  - quote 對得上主人行 → 寫入，history 有 `by`、`quote`。
  - quote 只出現在 `Me:` 行、只出現在 action log 行、太短、根本沒有 → 拒絕，檔案不變。
  - section 不是 user／relationship → pydantic 拒絕。
  - replace 的 target 在 self 區 → 拒絕（同 Task 2 的檢查）。
  - consolidation 失敗或沒有 transcript → keeper 不跑。
  - `enabled = false` → 不跑。
  - keeper 的工具 JSON 不出現在 Doll 的 offered 清單（golden 不變）。

### Task 5 — 對 Strata 的驗收（spec §5）

- 照 harness live 驗收的設定（`docs/research/2026-10-27-harness-live-acceptance.md`：新的空資料目錄、語音和 Discord 關掉、`ws_client.py` 驅動、IPC `ChannelEvent` 送私訊和陌生人訊息）。
- spec §5 的 7 個情境，每個 3 次。情境 3、4 直接呼叫 `run_profile_keeper`（不用等閒置觸發）。
- 過關照 spec §5：1、2、5 每次都過；3 至少 2/3；4、6、7 每次都過。
- 記下：工具 JSON 改了之後第一次的 cache 重算代價、keeper 花多久、用了多少 token。
- 寫成 `docs/research/2026-10-27-remember-corrections-live.md`（或實際跑的日期），給你看。沒過的情境照實寫，不在這個 task 裡改 prompt 硬湊。

### Task 6 — 文件

- spec 狀態補上「已實作（plan 2026-10-27）」。
- `CLAUDE.md`：「下一個」拿掉「記住糾正」；Key Architecture Decisions 加一條「主人教的事」：當下 PinSelf（user／relationship，主人在場的對話回合）＋每天 profile keeper（要引用主人原話）。

---

## 驗收

- Task 0 的基準 + 新測試全綠。
- golden 只在 Task 3 變，而且只變 PinSelf 的說明。
- Task 5 照 spec §5 過關，報告寫好。
- 合併到 main 前先給你看 Task 5 的報告。

## 不在範圍內

照 spec §4：一次性事實的新舊筆記並存、工具用法的糾正（`NoteToolLesson`）、糾正分類器、陌生人的糾正、風格規則量化。另外：workflow worker 的 Shell 結果回錯地方（另一個 session 的任務）、`PersonaDriftDetected` 引用陌生人回合的話（等你決定陌生人回合要不要檢查客服腔）。
