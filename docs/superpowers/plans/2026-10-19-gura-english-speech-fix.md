# Gura 在私人對話裡講英文：把 daemon 自己的框架句換成英文

**Spec**：`docs/superpowers/specs/2026-10-18-dollos-redesign-design.md` §8（她講英文）、Q7（她寫的東西維持繁中）；問題來源 `docs/research/2026-10-18-strata-first-numbers.md` finding 1、表格「回覆是英文」那列
**日期**：2026-10-19
**狀態**：APPROVED（使用者 2026-10-19；mood 選英文）
**分支 / worktree**：DollOS `feat/gura-english-speech`，`.worktrees/gura-english-speech/`，疊在 `feat/model-adapter-chat`（`c931d1c`）上面。這份 plan 現在放在主 checkout、還沒 commit；Task 0 開分支時搬進去。

---

## 查證後的現況

- **症狀**（live run 2026-10-19）：私人對話每一回合都講中文（一開始繁體，換頁後混進簡體，例如「挪威北边」）；外部 `ch:` 對話講英文。24 個 trace 的思考（reasoning）全部是英文，只有說出口的話變中文。
- **system 不是主因**：private 和 `ch:` 的 system 一字不差（`mind_loop.py:370` 的 `_system_for_turn` 不分 tier）。`ch:` 的 user 訊息中文框架反而更多，她還是回英文，因為最新一則是英文的人話。
- **第一句就是中文**：第一個回合是 Awoke（沒有主人的話可以跟）。user 訊息裡唯一的自然語言是 `mood: 平靜`、`精力: 飽滿`，她說「早安。今天是星期一,我剛醒過來,一切平靜。」之後整頁跟著中文走：開機排程塞進來的 ScheduledMoment 是中文命令（`kernel.py:1055`「今天還沒有計劃，請呼叫 WriteSchedule 安排今天的行程。」，也違反 Self-First），她寫的排程是中文，到主人說 "Hi Gura, what are you up to?" 時她背後已經有兩句中文，回「Hi! 我剛醒…」。
- **跨頁、跨重啟把中文帶下去的**：新一頁 snapshot 的 `[Recent outputs]`（約 15 句她講過的話）；`[Recent perceptions]`（開機排程那句存在 `mind_state.json`，run C 重啟後還在）；Recall 和 `[Memory context]` 回來的逐字稿（`主人說：…`／`我說：…`）和 action log。
- **離線重現**（腳本和結果已存進 `docs/v1-iphone-body` 分支的 `experiments/lang-exp/`；用真的 `DollOS` kernel 建 request，model 換成只記錄不送出；system hash `ae7fede…`、tools hash `501847…` 跟 live 的 epoch_start 一樣；Strata，30 個 request，每個變體只改一件事）：

  | 變體 | 改了什麼 | 跑幾次 | 有講話的回覆 |
  |---|---|---|---|
  | A 原樣，Awoke | 無 | 3 | 中文 2/2 |
  | A 原樣，主人說英文 | 無 | 4 | 英文 4/4 |
  | B | 只把 mood／精力 標籤換英文 | 3 | 中文 3/3 |
  | C | 只拿掉 `[Memory guideline]` | 3 | 中文 1/1 |
  | F | 只把工具說明換英文（681 個漢字 → 0） | 6 | 英文 1/1 |
  | D | 標籤、system 中文段落、工具都換英文；guideline 原樣 | 3 | 英文 1/1 |
  | E | D + guideline 改成英文描述句 | 3 | 英文 1/1 |
  | E，主人說「你在幹嘛？」 | | 2 | 英文 2/2 |

  工具說明有中文（A/B/C）：有講話的 6 次全中文；工具換英文（D/E/F）：3 次全英文（Fisher exact p≈0.012）。F 只換了工具，system 的中文、標籤（包括 `mood: 平靜`）、guideline 都還在，她講英文。外部 `ch:` 的工具只有 3 個（66 個漢字），live 時那裡本來就英文。
- **量**：private 第一個 request 7571 token，漢字約 800 個（工具 681、system 113、`[Mind state]` 6），其中約 615 在只有 internal tier 才有的工具說明（PinSelf 203、SelfRevision 189、LearnName 137、PursueGoal 54、AdvanceGoal 15、Shell 9、MoodTool 6、SpawnMonitor 2）。
- **限制**：Awoke 常常只呼叫工具不講話（工具換英文的 12 次裡只有 3 次講話，原樣類 9 次裡 6 次），英文變體各只有 1 句能判斷；分不出哪個工具的中文最關鍵。沒測到的：開機排程那句、ReflectionMoment 內文、system 裡採納後的中文「現在的我」、舊資料夾裡的中文 `[Recent outputs]`、英文框架下她寫的筆記和日記還是不是中文。
- **明確的語言規則有三條**：personality 的 "I understand both Chinese and English, but I talk in English."（她的身份，要保留）；`[Memory guideline]` 要她用繁體中文寫（Q7，保留但改寫）；`MoodTool.emotion` 的 "one Chinese word (e.g. '開心')"（`tools.py:894`，讓 mood 標籤永遠是中文）。mood 是中文這件事是照 Q7 設計的：Q7 列的「改英文要跟著改」裡有「mood 的 emotion ≤ 6 字」，已核可的 mind-loop spec（`2026-10-17-mind-loop-redesign-design.md:297`）也打算在 pydantic 層把 emotion 限制成 ≤ 6 個字、不能有空白和標點。F 裡 `mood: 平靜` 還在，她照樣講英文，所以這個 plan 預設不動它（見「要你決定」）。
- **簡體**：prompt 沒有任何地方說「說話用繁體」，只有記憶寫作有。她一講中文，第二頁就混進簡體，再被 `[Recent outputs]` 重播。說話變英文後這條路就斷了；筆記和日記由 guideline 的 "Traditional Chinese" 管。這個 plan 不加轉換器，只在驗收量。
- **「主人」到處都是**：system 3 處、NoteMemory／PinSelf／LearnName 說明、ReflectionMoment 內文、self-profile 段落標題、逐字稿的 `主人說`、`[Body signal]`。英文身份說 "my owner"。

**這個 plan 要做的**：daemon 自己寫的字（工具說明、system 框架、標籤、感知框架、工具回傳、寫進記憶的逐字稿和 action log 標籤）在 private 這條路上全部換成英文，「主人」譯成 "your owner"。開機排程那句命令改成英文的描述。不加任何「要用英文回」的命令：她講英文這件事已經寫在她的身份裡。

**留著中文的只有她自己的內容**：她寫的筆記、日記、議程、scratchpad、排程內容、Recall 結果、採納的「現在的我」、self-profile 條目；設定裡的 `primary_language` 值；scaffolding 那句範例筆記「主人要我記得：明天要吃早餐」（照 Q7 示範「講英文、筆記寫中文」；D、E 實驗把它翻成英文，F 實驗保留中文，都講英文）；記憶檔的 `日記`／`計劃` 標題（見不在範圍內）。

**prefix 規則**：system 和工具一改，每段對話在部署後開一次新頁（`context_builder.py:152-153` 比對 tools／system hash，不同就開新頁；對話只存在記憶體，重開本來就開新頁；不用寫遷移）。Strata 那邊要一次冷 prefill，約 31 秒。

**已決定（2026-10-19）：mood 用英文**。要多做三件事（已併進 Task 2、Task 4）：`MoodTool.emotion` 說明改成「一個英文字」（例如 'happy'、'uneasy'），用 pydantic 限制成一個英文字、≤ 12 個字母、沒有空白和標點；預設 mood 改成 `calm`；mind-loop spec §10 的「≤ 6 個字、不能有空白」要改成英文版，列在下面「Spec 修正」。舊記憶標的 `mood:平靜` 不會再跟新的 mood 聯想到（這台機器沒有舊資料，影響不大）。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個測試套件全綠。英文譯文優先用實驗用過的 `experiments/lang-exp/variants.py`（`TOOLS_EN`、`SYSTEM_EN`），找不到就照原意重譯。除了驗收，都不需要 LLM 主機。

- **名詞統一**（每個 task 是不同的 subagent，要用同一套譯名）：「主人」→ "your owner"；「現在的我」→ "who I am now"（system 標題是 `## Who I am now`，其他地方照這個詞）；`[人格演化候選]` → `[Self revision candidate]`。
- **釘舊字串的測試不只每個 task 列的那些**：改一個字串前先 `grep -rln '<舊字串>' tests` 找全（例如 `test_turn_delta.py`、`test_consolidation.py`、`test_self_revision.py`、`test_pin_self.py`、`test_evolution_*.py`、`test_config.py` 都有釘中文）。

### Task 0 — 開分支、記基準

- 從 `feat/model-adapter-chat` 開 `feat/gura-english-speech`（`.worktrees/gura-english-speech/`），把這份 plan 搬進去。
- 從 `docs/v1-iphone-body` 把 `experiments/lang-exp/` 的 `capture.py`、`run.py`、`variants.py`、`results.jsonl` 帶進新分支（`git checkout docs/v1-iphone-body -- experiments/lang-exp/{capture.py,run.py,variants.py,results.jsonl}`；`captured.json` 不帶，`capture.py` 會重建）。`capture.py` 裡寫死的 worktree 路徑改成新分支。
- `uv sync`、`uv run pytest -q`，**記下基準**（幾個通過、幾個失敗）；原本就失敗的列給你看，不在這裡修。

### Task 1 — 守門測試：private 的 prompt 只准出現允許的中文

- 檔案：新增 `tests/_cjk.py`、`tests/test_private_prompt_language.py`
- `_cjk.py`：`cjk_runs(text) -> list[str]`，抓連續的漢字（`㐀-鿿`、`豈-﫿`）和全形標點（`　-〿`、`＀-￯`）；`assert_only_allowed(text, allowed)`：先把每個允許字串**整串**從 text 裡拿掉，剩下的不准有任何 CJK，失敗時印出那段和前後文。不能用「每段都是某個允許字串的子字串」：範例筆記含「主人」，scaffolding 裡沒改到的「主人」會被放過，測試永遠不會紅。
- 用真的 `DollOS(settings)` 拿 MindLoop（gura pack、暫存 data root、`[evolution]` 和 `[self_profile]` 開著；跟 `experiments/lang-exp/capture.py` 同一種建法，`tests/test_kernel.py` 的 `_make_settings` 可參考）。不要用 `tests/_mindloop_factory.py`：它的 system 預設是 `"SYS"`，測不到 scaffolding；`[人格演化候選]` 那段要 evolution 開著才會渲染，PinSelf／SelfRevision 要 self_profile／evolution 開著才在工具清單裡。她自己的內容一律填哨兵字串 `她的字`（mood、議程、scratchpad、self-profile 條目、記憶 hit、排程 intent、現在的我、候選內文、skeptic 的 notice）。允許清單 = 哨兵、`primary_language` 的值、範例筆記那一句。每個來源一個參數化 case：
  - system（`_system_for_turn()`，有和沒有採納的現在的我各一）
  - internal 和 external_public 的工具 schema（`json.dumps(..., ensure_ascii=False)`）
  - 新一頁的 snapshot：預設的 `MindState()`（不填 mood）一個，填滿哨兵的一個
  - delta：Awoke、UserSpoke、開機排程（同一個 kernel 跑 `_maybe_bootstrap_plan`，從 queue 拿出那個 perception 來渲染）、ReflectionMoment（含 `[人格演化候選]` 區塊和新舊標記）、PersonaDriftDetected、BridgeDown、McpDown、PulseMoment + `[Body signal]`、ToolResultArrived、`[Tool habits]`、有 provenance 前綴的記憶 hit
  - 日記 request（`render_diary_request` + `[Today's log]`）
  - 工具回傳：未知工具、參數錯誤、Shell／Workflow／SpawnMonitor dispatched、InvokeSkill 找不到、PinSelf／SelfRevision／LearnName 的成功和失敗訊息
  - 寫進記憶的標籤：`memory_writer` 的逐字稿行、action log 行（記憶檔的 `日記`／`計劃` 標題不改，見不在範圍內）
- 現在會失敗的 case 標 `pytest.mark.xfail(strict=True)`，並註明由哪個 task 修。後面每個 task 修好就拿掉自己的 xfail（strict 保證修好了卻忘了拿掉會變紅）。
- 驗收：這個 task 結束時測試套件是綠的，xfail 清單就是後面 Task 2–8 的範圍。

### Task 2 — 工具說明換英文

- 檔案：`src/dollos/tools.py`：NoteMemory `:151-154`、Shell `:220`、SpawnMonitor `:445`、PursueGoal `:801-807`、AdvanceGoal `:860`、PinSelf `:936-953`、SelfRevision `:981-996`、LearnName `:1168-1176`（docstring 和 `Field(description=...)` 都算）
- 改法：照 `TOOLS_EN` 翻；「主人」→ "your owner"。`MoodTool.emotion`（`:894`）改成 "one English word (e.g. 'happy', 'uneasy')"，並加 pydantic 驗證：只有英文字母、1–12 個、沒有空白和標點，違反就回清楚的錯誤（不要照抄 `TOOLS_EN` 那句有空白的 "short phrase"）。Shell 和 SpawnMonitor 說明裡過時的「你執行的 shell 命令回來了」「monitor 觸發」改成真的感知行開頭（`ToolResultArrived`、`MonitorFired`）。LearnName 的範例暱稱「小鯊」換成英文的（"Sharky"）。
- 測試：Task 1 的工具 schema case 拿掉 xfail；`tests/test_tools.py`、`tests/test_tool_schema.py` 裡釘中文說明的斷言跟著改。

### Task 3 — system：scaffolding、記憶 guideline、現在的我的框架

- 檔案：`src/dollos/prompts/templates/scaffolding.jinja:35-36、46-53、70`；`src/dollos/mind/mind_prompt.py:350-365`；`src/dollos/config.py:76`、`src/dollos/mind/mind_loop.py:178`、`config.example.toml:73`；`src/dollos/mind/current_self.py:16-19、32`
- 改法：
  - scaffolding 照 `SYSTEM_EN`：「主人」→ "your owner"；`[人格演化候選]` 改名 `[Self revision candidate]`（Task 5 的 ReflectionMoment 內文和 `render_surfacing` 標題要同一個名字）；「已經記下了」→ "got it, noted"。第 78 行的範例筆記維持中文（理由見現況）。
  - `render_memory_guideline` 整句改成描述（E 的版本前半照用；E 的後半 "Record … never copy …" 還是命令，改成描述）：`[Memory guideline] Your notes and diary are written in {primary_language} (proper nouns and technical terms may stay in their original language), in your own words — you understand something first and write what it means to you; you don't copy source text verbatim.` 位置不動（system 最後）。後半跟 E 測過的字不一樣，驗收重跑會蓋到。
  - `primary_language` 的預設改成 `"Traditional Chinese"`（三個地方）。你 PC 上的 `config.toml` 若寫 `"繁體中文"` 也能用（D 實驗就是這樣），但建議跟著改。
  - `current_self` 的標題改成 `## Who I am now`，框架句翻成英文描述（"a description, not a command"，意思不變）。採納的內文照 Q7 維持中文。
- 測試：Task 1 的 system case 拿掉 xfail；`tests/test_prompt_renderer.py`、`tests/test_current_self.py`、`tests/test_mind_prompt.py`、`tests/test_config.py`（預設值）裡的舊字串跟著改；`render_memory_guideline("Traditional Chinese")` 含 `written in Traditional Chinese`，不含 `write it in`、`Record your`、`never copy`（命令句不准回來）。

### Task 4 — `[Mind state]` 和 snapshot 的標籤

- 檔案：`src/dollos/mind/mind_prompt.py:35-48`、`src/dollos/mind/self_profile.py:20-22、230-245`
- 改法：
  - 預設 `Mood.emotion`（`mind_state.py:43`）改成 `"calm"`；`test_mind_loop_action_log.py:99、119`、`test_associative_recall.py`、`test_kernel_query_handler.py` 釘的 `平靜` 跟著改；已存的 `mind_state.json` 不動（裡面舊的中文 mood 會原樣顯示，直到她下次更新 mood）。
  - `energy_bucket_line`：`energy: full / normal / low (0.x)`，docstring 跟著改。
  - `[Self profile]` 渲染時用英文標題（`## What I've learned about myself`、`## Me and my owner`、`## What I've noticed about my owner`）：另加一張英文的顯示標題表，`render_block` 和 Task 7 的 PinSelf 回傳訊息（現在用 `SECTION_TITLES`）都用它。`SECTION_TITLES`、`_TITLE_TO_SECTION` 和檔案裡的中文標題不動，所以舊檔照讀、字數上限照舊算，不用遷移。
- 測試：snapshot case 拿掉 xfail；`tests/test_energy.py`、`tests/test_mind_prompt_self_profile.py`、`tests/test_self_profile_store.py`、`tests/test_turn_delta.py`（`精力`）跟著改；加一條「舊的中文標題檔案讀得起來、渲染出英文標題」。

### Task 5 — 感知框架和每回合的 daemon 區塊

- 檔案：`src/dollos/mind/mind_prompt.py`（`_percep_body` 的 ReflectionMoment `:525-541`、PersonaDriftDetected `:560-566`、BridgeDown／McpDown `:590-600`、DiaryMoment `:601-605`；`render_body_signal` `:671-694`）、`src/dollos/mind/pulse_observer.py:129-142`、`src/dollos/mind/mind_loop.py:1227-1228`（`[Today's log]` 標題、「中段略」）、`src/dollos/mind/tool_memory.py:107`、`src/dollos/mind/evolution.py:246-272`、`src/dollos/mind/surfacing_markers.py:7-8`
- 改法：照原意翻成英文。原本就是描述的維持描述；ReflectionMoment 和 DiaryMoment 只翻譯，內容不改（改寫成 Self-First 是另一件事，見不在範圍內）。PersonaDriftDetected 拿掉「下次注意」這個命令，只描述發生了什麼；`"、".join` 改成 `", "`。`render_surfacing` 的區塊標題改成 `[Self revision candidate]`，ReflectionMoment 內文提到的區塊名跟著改，舊／新候選內文（她的內容）照原樣。候選前面的標記 `【現行·舊】`／`【候選·新】` 也是 daemon 的字，換成英文、夠特別、不會出現在她自己文字裡的（例如 `⟪current⟫`／`⟪proposed⟫`）；`strip_surfacing_markers` 和 echo 比對讀同一個常數，不用另外改。
- 測試：對應的 delta、日記 case 拿掉 xfail；`tests/test_evolution_surfacing.py`、`tests/test_self_revision.py`（標記）、`tests/test_mind_loop_diary_turn.py`、`tests/test_tool_memory.py`、`tests/test_mind_loop_pulse.py`、`tests/test_mind_prompt.py` 跟著改。

### Task 6 — 開機排程：命令改成描述

- 檔案：`src/dollos/kernel.py:1030-1057`（`_maybe_bootstrap_plan`，那句在 `:1055`）
- 改法：intent 改成英文描述，例如 `"A new day. Nothing is on today's schedule yet."`，不提 WriteSchedule、不叫她做事。要不要排今天的行程由她決定。
- 測試：`tests/test_kernel.py` 新增：當天沒有排程檔時，推進 queue 的 perception 的 intent 沒有 CJK、不含 `WriteSchedule`；有排程檔時什麼都不推。Task 1 的開機排程 case 拿掉 xfail。

### Task 7 — 工具回傳和記憶 hit 前綴

- 檔案：`src/dollos/tools.py:134-136、254、289-292、434、503、1014-1148、1189-1201`、`src/dollos/cascade/tool_loop.py:85、100-102`、`src/dollos/mind/self_profile.py:134-221`、`src/dollos/mind/evolution.py:366-371`、`src/dollos/mind/mind_prompt.py:382、423`（`evolution.py:251` 在 Task 5 的範圍裡）
- 改法：翻成英文。LearnName 的成功訊息原本是她的口吻（「記住了,之後你也可以叫我…」），英文也維持她的口吻。InvokeSkill 找不到時只說「這個 skill 不存在，現在有的是…」，拿掉「建議…不要再猜」。PinSelf 的「已 pin 到「…」」用 Task 4 的英文顯示標題。前綴改成 `[external AI · unverified]`、`[consolidated · unconfirmed]`（查過：程式裡沒有靠中文前綴判斷的，只有測試釘字串）。
- 測試：工具回傳 case 拿掉 xfail；`tests/test_tools.py`、`tests/test_tool_loop.py`、`tests/test_recall_scope.py`、`tests/test_memory_provenance_external_public.py`、`tests/test_consolidation.py`（`[系統整併·待確認]`）、`tests/test_pin_self.py`、`tests/test_self_revision.py`、`tests/test_evolution_checks.py` 跟著改。

### Task 8 — daemon 寫進記憶檔的標籤

- 檔案：`src/dollos/memory_writer.py:44-49`、`src/dollos/mind/action_log.py:27-81`（含 docstring）
- 改法：新寫的行改成英文（`Owner: …`／`Me: …`、`▸ noted: …`、`▸ scheduled 4 things`）。查過：程式只靠 `▸` 前綴分辨 action log 行（`memory_writer.py:23、56`），沒有程式解析「主人說」「我說」，所以不用讀舊格式；舊的記憶檔不動。
- 測試：記憶標籤 case 拿掉 xfail；`tests/test_action_log.py`、`tests/test_mind_loop_action_log.py`、`tests/test_episodic_transcript.py`、`tests/test_memory_writer.py`、`tests/test_consolidation.py`、`tests/test_mind_loop_diary_turn.py` 跟著改。

### Task 9 — 收尾

- Task 1 的檔案裡不准再有 xfail；整個套件對 Task 0 的基準全綠。
- `grep -rnP '[\x{4e00}-\x{9fff}]' src/dollos` 跑一次，剩下的每一處都要能歸進「不在範圍內」的某一條（ch:、agent prompt、註解和 docstring、日誌和給 client 的錯誤訊息、處理中文的程式、她的內容、mood、記憶檔標題），清單貼給你。

---

## Spec 修正

- **mind-loop spec §10**（`2026-10-17-mind-loop-redesign-design.md:297`）：emotion 的限制從「≤ 6 個字、不能有空白和標點」改成「一個英文字、≤ 12 個字母、不能有空白和標點」（使用者 2026-10-19 選英文 mood）。
- **redesign spec Q7**：「改英文要跟著改」清單裡的 mood 一項已經改了；她的筆記、日記、現在的我仍是繁體中文。

---

## 驗收

- **測試**：Task 0 的基準加新測試全部通過（原本就失敗的除外），守門測試沒有 xfail。
- **離線重跑**（Strata；用 `experiments/lang-exp/` 的 capture／run 腳本，`capture.py` 讀新分支）。跟上次一樣客氣：每個 request 前看 `/health` 和 `/metrics`，有人在用就等；`max_tokens` 400、`reasoning_budget_tokens` 256；總數不超過 30 個，結果明確就提早停。
  - 情境：Awoke（講話的回覆湊到 3 句，最多 8 個 request）；開機排程那個回合 ×3；ReflectionMoment ×2；主人說英文 ×2；主人說中文 ×2；system 裡有一段約 400 字中文「現在的我」，Awoke 和主人說中文各 ×2；日記 request ×2。
  - 判定跟上次一樣（`run.py` 的 `classify`）：去掉 "Gura" 後，漢字數 ÷（漢字數 ＋ 拉丁字母數 ÷ 4）< 0.2 算英文，> 0.8 算中文，中間算混合，只呼叫工具的另外記。
  - 過關：上面所有情境中，**有講話的回覆 100% 英文**，0 句中文、0 句混合；而且**她寫的東西照 Q7 還是繁體中文**：日記 request 的 `WriteDiary` 內容、任何一次 `NoteMemory` 的內容都算中文（框架全換英文後，日記拿到的 `[Today's log]` 也大多是英文，這是最可能悄悄改掉 Q7 的地方）。
  - 另外量、不算過關條件：反思回合裡 `PinSelf` 寫的是什麼語言；拿 live run 的舊資料夾（run B 第二頁，`[Recent outputs]` 是中文）跑 Awoke ×3。舊資料夾還是回中文，或 PinSelf 變英文，回來跟你討論；不自動刪她的記憶。
- **live 跑一次**（新的 data root，private 約 10 個回合，跟 strata-first-numbers 那次同一套：主人的英文短句、一句中文、一句「幫我記一件事」；加 2 個 `ch:` 回合）：
  - private 有講話的回覆英文比例要 100%；`ch:` 維持英文。
  - 她記下的筆記是繁體中文，簡體專用字 0 個（只在 scratchpad 用一份常見簡體字表或 opencc 檢查，不加依賴）。
  - 記下她有沒有自己排今天的行程（開機排程不再叫她排）；這只記錄，不算過關條件。
- 數字補進 `docs/research/2026-10-18-strata-first-numbers.md` finding 1。

## 不在範圍內

- 只有 `ch:` 會看到的中文：`[External situation]`（`mind_prompt.py:51-54`）、`[主人私訊]`／陌生人／`[AI peer 私訊]` 框架（`:567-589`）、除錯通道的「請務必給出實質回覆」（`:580`）。live 時 `ch:` 本來就講英文；主人用中文傳 Discord 私訊還沒測，回中文的話另寫一份 plan。
- 把 ReflectionMoment、DiaryMoment 改寫成 Self-First 的描述（這裡只翻譯；例外是開機排程、PersonaDrift 的「下次注意」、InvokeSkill 的建議句這三處命令直接拿掉）。
- 她自己寫的東西的語言（Q7）：筆記、日記、議程、「現在的我」、self-profile 條目，和已經存在的記憶檔、`mind_state.json`。
- evolution keeper／trigger skeptic／consolidation 的 agent prompt（是另外的 request，照設計用中文）。skeptic 的 KILL 理由（agent 的輸出，或 `evolution_trigger.py:219、278` 的預設）會出現在候選區塊的 notice 裡，跟著維持中文。
- 記憶檔的 `## … 日記`／`## … 計劃` 標題（`tools.py:207、626`）：`evolution.py:494-535` 的 `has_diary_heading`／`diary_days_since` 和 `evolution_keeper.py:102` 靠「日記」找日記，Mode A 演化靠它觸發、keeper 靠它收證據；演化的測試用寫死的中文標題，改了寫入端測試照過、正式環境卻再也找不到新日記。進 prompt 的只有 Recall 結果裡的兩個字，不值得。
- mood 字（見「要你決定」）。
- 不進 prompt 的中文：日誌、trace、給 client 的錯誤訊息（例如 `tool_loop.py:310-311` 的 `ErrorMsg`）；處理中文的程式（斷句、FTS、`persona_guard`、`name_aliases` 的停用字）。
- 自動簡轉繁（opencc）：驗收量到筆記有簡體才另外做。
- 死碼：STALE 標記（`mind_prompt.py:450`）、`_render_perceptions`、`_render_outputs_header`、`render_tool_notes`，這裡不刪。
- `doll.toml` 的 `restatement_reference` 和 `aliases = ["古拉"]`（不進 prompt）；yesman、powdur；語音。
