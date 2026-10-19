# Doll 寫的東西也改成英文（Q7 反轉）

**Spec**：`docs/superpowers/specs/2026-10-18-dollos-redesign-design.md` Q7（`:521`，原本選繁中，現在改英文）、`:378`；演化 `docs/superpowers/specs/2026-07-02-slow-self-evolution-design.md` §3.3；前一份 plan `2026-10-19-gura-english-speech-fix.md`（說話已改英文）
**日期**：2026-10-19
**狀態**：APPROVED（使用者 2026-10-19「全改成英文吧」）
**分支 / worktree**：繼續用 DollOS `feat/gura-english-speech`（`.worktrees/gura-english-speech/`，HEAD `836d4dd`），不開新分支。

---

## 查證後的現況

- **已經是英文**：她說的話、mood（一個英文字，預設 `calm`）、所有標籤和框架、`[Today's log]`、逐字稿的 `Owner`／`Me`、self-profile 的顯示標題（前一份 plan）。上次驗收裡 `PinSelf` 條目、議程、排程、focus 本來就寫成英文。
- **還是中文的寫作**：筆記、日記、「現在的我」。靠三樣東西：`[memory] primary_language = "Traditional Chinese"`（`config.py:76`、`config.example.toml:73`、`mind_loop.py:178`、`tests/_mindloop_factory.py:51`）、`[Memory guideline]`（`mind_prompt.py:350-365`，只講「notes and diary」）、scaffolding 的中文範例筆記（`scaffolding.jinja:79`）。日記裡會混進簡體字，改英文後這個問題就沒了。
- **產生她寫作的 agent prompt 是中文**：演化 keeper（`evolution_keeper.py:23-44`，「繁體中文,{floor}–{cap} 字」，格式標記 `依據`，`parse_keeper_report` `:127-151` 靠它切）、完整 skeptic（`:46-74`，寫死「600 字」）、Mode B skeptic（`evolution_trigger.py:39-58`；KILL 理由會變成 `slot.notice`，在 `[Self revision candidate]` 裡給她看）、consolidation（`consolidation.py:22-25`）。佔位字、預設理由和證據區塊標籤也是中文（`evolution_keeper.py:108-116、142、175-176、190、215、255-257`，`evolution.py:576`，`evolution_trigger.py:203、219、278`）。agent 的輸出語言跟著輸入走：consolidation 讀的是逐字稿，主人說中文時，只把 prompt 翻成英文，抽出來的事實還是會寫成中文。
- **兩個檢查遇到英文會壞**（在這個分支本機跑過）：
  - C1 `_normalize_echo`（`evolution.py:208-221`）把空白全拿掉，jieba 把整段英文當一個詞，Jaccard 那條變成只有全等才算。兩句幾乎一樣的英文：原始 Jaccard 0.889，`echo_equivalent` 卻是 False。她換句話說的「採納」會被當成反提案再審一輪（安全，但多一輪）。
  - C2 `_SENTENCE_SPLIT_RE = r"[。!?;?!;\n]+"`（`:470`）：bytes 是 `。` 加 ASCII `!?;?!;`，沒有 `.`，也沒有全形 `！？；`。整段英文變成一句，候選裡一字不差抄了 pack 的 "My mood is steady." 也抓不到（回 None）；改成 `[。！？；.!?;\n]+` 就抓到。
- **`restatement_reference` 不再需要**：它存在只因為 keeper 寫中文（`doll.toml:36-55`、`character.py:44-53`、`evolution_keeper.py:248-252`）。keeper 改寫英文、C2 修好後，直接跟英文 pack 比。本機量過：抄原句 1.0、部分重述約 0.73、換句話說 0.2–0.55、新內容 0.15–0.25，門檻 0.6 照用；換句話說的重述本來就是 skeptic (c) 的事。只有 Gura 設了這個欄位，同一個 commit 拿掉不會讓任何 pack 載入失敗。
- **daemon 寫進她檔案的中文標題**：
  - `WriteDiary` 寫 `## {heading} 日記`（`tools.py:208`）；`has_diary_heading`（`evolution.py:501-507`）靠「日記」認日記，`diary_days_since`（Mode A 的觸發條件）和 keeper 收證據（`evolution_keeper.py:102`）都用它。
  - `WriteSchedule` 寫 `## {heading} 計劃`（`tools.py:622`），沒有程式讀它，只進搜尋索引。
  - `self_profile.md` 的段落標題是中文（`self_profile.py:19-36`），`_parse` 只認 `_TITLE_TO_SECTION` 裡的標題。**換成英文標題時如果不保留中文對照，舊檔會讀成空的，下一次 `PinSelf`（讀→解析→寫回）就把舊條目全部蓋掉。**
- **字數上限是照中文訂的**：「現在的我」80／600（`config.py:282-283`、`mind_ctx.py:98-99`、`mind_loop.py:189-190`、`evolution_trigger.py:78`），self-profile 1200（`config.py:267`、`mind_ctx.py:52`）。同樣內容英文約是中文的 3.3 倍字元（Gura pack：英文 1709、中文 511）。長度只檢查新寫的字：已採納的「現在的我」在 `process_tripwire` 是 `in_sync` 就直接 return（`evolution.py:419-420`），不會重驗。
- **`ch:` 回合還有 daemon 的中文**：`_EXTERNAL_SITUATION`（`mind_prompt.py:51-54`）、ChannelMessage／MCP 框架（`:576-592`：`[AI peer 私訊]`、`(另一個 AI,自稱,未驗證)`、除錯通道那句、`私訊`、`[主人…] 主人:`、`陌生人`）。死碼兩處：STALE 標記（`:450`，唯一呼叫端傳 `absolute=True`）、`render_tool_notes`（`tool_memory.py:124`，沒有呼叫端）。
- **舊資料**：這台機器沒有；另一台可能有中文記憶。讀舊檔要照常（這是相容，不是 fallback）：中文 `日記` 標題、中文 self-profile 標題、中文 consolidated、舊的中文「現在的我」。不轉換、不遷移舊檔。

**prefix**：system（guideline、範例筆記）一改，部署後每段對話開一次新頁，Strata 一次冷 prefill，跟上次一樣不用寫遷移。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改程式，最後整個套件全綠。除了驗收都不碰 LLM 主機。改一個字串前先 `grep -rln '<舊字串>' tests` 找全釘它的測試。譯名沿用前一份 plan："your owner"、"who I am now"、`[Self revision candidate]`。

### Task 0 — 基準

- `uv sync`、`uv run pytest -q`，記下通過／失敗數（上次 1769 passed）。原本就失敗的列出來，不在這裡修。

### Task 1 — 守門測試：不准有任何中文，除了她的哨兵

- 檔案：`tests/test_private_prompt_language.py`
- `allowed` 只剩哨兵 `她的字`：拿掉 `EXAMPLE_NOTE` 和 `primary_language`，改寫 docstring。
- 新 case（她的內容一律填哨兵）：
  - **她的記憶檔**：用 `_run_tool` 跑 `WriteDiary`、`WriteSchedule`、`PinSelf`，讀回 `shared/{date}.md` 和 `self_profile.md` 整個檔案（daemon 寫的標題要英文）→ Task 3 修。
  - keeper 的 `_KEEPER_TASK`、`_FULL_SKEPTIC_TASK`、Mode B `_SKEPTIC_TASK`、consolidation `_KEEPER_TASK`（`.format` 填哨兵）；keeper 重試的回饋句（照 `test_evolution_keeper.py:279` 的作法 monkeypatch `_run_keeper_agent` 收 task 文字）→ Task 5 修。
  - `assemble_bundle` 在暫存 memory root 上組出來的證據區塊，檔案用上面那些工具寫（標籤和檔案標題都要英文）→ Task 3 和 Task 5 都要改，Task 5 拿掉 xfail。
  - ChannelMessage 的主人私訊、主人在頻道、陌生人、AI peer、除錯通道；`external_public` 的 `[External situation]` → Task 7 修。
- 現在會失敗的用 `pytest.param(..., marks=pytest.mark.xfail(strict=True, reason="Task N"))` 標起來；修好的 task 拿掉自己的 xfail。

### Task 2 — 寫作語言：設定、guideline、範例筆記、pack

- 檔案：`config.py:69-76`、`config.example.toml:66-73`、`mind_loop.py:178`、`tests/_mindloop_factory.py:51`；`mind_prompt.py:350-365`；`scaffolding.jinja:77-79`；`character_packs/gura/doll.toml:26`
- 改法：
  - `primary_language` 預設改 `"English"`（四處，`tests/test_config.py:253` 會比對），`config.py:69-73` 的 docstring 和 `config.example.toml:66-69` 的註解從「NoteMemory / diary」改成她寫下的所有東西。設定本身留著：`[memory]` 是 `extra="forbid"`，拿掉會讓既有 `config.toml` 載入失敗。**你 Mac 上的 `config.toml` 如果寫了 `primary_language`，要改成 `"English"` 或刪掉那行**，不然她繼續寫中文。
  - guideline 擴大成描述：`[Memory guideline] Everything you write down — notes, diary, who I am now, self-profile, agenda, schedule — is in {primary_language} (proper nouns and technical terms may stay in their original language), in your own words — …`（後半「own words」照舊；"who I am now" 是因為她用 `SelfRevision` 改寫的也是她寫的字）。
  - 範例筆記改 `"My owner wants me to remember: breakfast tomorrow"`。
  - personality 最後一句改 "I understand both Chinese and English, but I talk and write in English."（描述，符合 Self-First）。
- 測試：`test_config.py:243-250` 預設值；`test_mind_prompt.py` 加擴大後的 guideline case；`test_prompt_renderer.py:434-451、464-476` 改成 system 裡「完全沒有 CJK」；Task 1 的 system case 拿掉 xfail。

### Task 3 — 記憶檔標題：新寫英文，讀新舊兩種

- 檔案：`tools.py:208、622`；`evolution.py:501-543`；`self_profile.py:19-61`
- 改法：
  - 日記標題 `## {heading} Diary`，排程摘要 `## {heading} Plan`。
  - `has_diary_heading`：`line.startswith("##") and ("日記" in line or line.rstrip().endswith(" Diary"))`，docstring 寫明中文是讀舊檔用的。
  - self-profile：`SECTION_TITLES` 改成英文（就是現在的 `SECTION_DISPLAY_TITLES`，兩張表併成一張）；另加 `_LEGACY_TITLES`（三個中文標題），`_TITLE_TO_SECTION` 兩種都認。舊檔第一次寫入時就變成英文標題。
- 測試：`test_tools.py:165、502-503、684` 改 `Diary`／`Plan`；`test_self_profile_store.py:14-16、129-130、340` 檢查寫出來的檔案，改英文標題；`:164` 跟著併表改。`test_self_profile_store.py:144` 那個舊檔測試改成：讀得出條目、`apply` 之後舊條目都還在、檔案只剩英文標題。**新增**：`has_diary_heading`／`diary_days_since`／`assemble_bundle` 對 `## … Diary` 和舊的 `## … 日記`（含 `日記 (12:34:56)`）都成立，`## 12:00` 這種筆記標題不成立。中文標題的這些測試不用改，留著當讀舊檔的測試：`test_evolution_integration.py:168`、`test_self_profile_integration.py:91`、`test_context_builder.py:387`、`test_evolution_mode_a_helpers.py:37-52`、`test_evolution_mode_a_trigger.py:104-105`、`test_evolution_keeper.py:23、35、54`。Task 1 的記憶檔 case 拿掉 xfail。

### Task 4 — 演化檢查支援英文（C1、C2）

- 檔案：`evolution.py:208-230、468-470`
- 改法：`_normalize_echo` 的全等那條照舊（去掉空白和標點）。Jaccard 那條改成只拿掉 surfacing 標記、做 NFKC，就交給 `pairwise_jaccard`。它本來就會轉小寫、空白壓成一格、去標點（`persona_guard._normalize_for_fingerprint`），不用再寫一個 normalize。中文只差空白的情況仍然由全等那條接住。`_SENTENCE_SPLIT_RE` 改 `r"[。！？；.!?;\n]+"`，註解照實寫。`ECHO_SIMILARITY`、`RESTATEMENT_THRESHOLD`、`RESTATEMENT_MIN_SENTENCE` 不動。
- 測試：`test_evolution_checks.py`／`test_self_revision.py` 的中文 case 留著，加英文：換句話說的 echo 算 equivalent；段落中間一字不差抄 pack 的一句會被 `restatement_overlap` 抓到；全新內容不會。

### Task 5 — 演化和 consolidation 的 agent prompt 改英文；拿掉 `restatement_reference`

- 先做完 Task 4：拿掉 `restatement_reference` 之後，重述檢查只比英文 pack，要靠 C2 的斷句才抓得到。
- 檔案：`evolution_keeper.py`、`evolution_trigger.py:39-58、203、219、278`、`evolution.py:576`、`consolidation.py:22-25`；`character.py:44-53`、`character_packs/gura/doll.toml:36-55`
- 改法：
  - 四個 prompt 照原意翻成英文，內容和規則不變。這些是給 agent 的工作說明，不是她的身份，本來就是指令句，不受 Self-First 限制。keeper 的長度改 "(English, {floor}–{cap} characters)"；完整 skeptic 的「600 字」改 `{cap}`（`_run_full_skeptic` 多傳 `cap`）。格式標記 `NO_CHANGE`／`CANDIDATE`／`Evidence:`。consolidation 的例子改 "My owner prefers X"（不是 "I am X"），並寫明每條事實用英文寫，因為主人說中文時逐字稿就是中文。Mode B skeptic 的 KILL 理由會變成 `slot.notice` 給她看，所以要求用英文寫一句理由。
  - `parse_keeper_report`：切在行首的 `\nEvidence`（同現在的規則），沒有前綴但有 `Evidence` 行一樣算候選；預設理由 `"not enough evidence"`。只認英文：它解析的是剛生成的輸出，不是存檔。
  - 佔位字、證據區塊標籤（`[self_history events]`、`[Diary {d}]`、`[consolidated · secondary {d}]`、`external_ctx=true (written while reading external content)`）、重試回饋句、預設理由（`evolution_keeper.py:142、190`，`evolution_trigger.py:219、278`）都改英文。
  - 拿掉 `restatement_reference`：pack 的整段、`Identity` 的欄位、keeper 的 append 和註解。
- 測試：`test_evolution_keeper.py` 的 `VALID`／`依據`／`ZH_RESTATING` 改成 `Evidence:` 和英文候選，刪掉 restatement_reference 那組；`test_character.py:136-150`、`test_prompt_renderer.py:243-255` 刪掉；`test_consolidation.py` 跟著改。Task 1 的 agent prompt 和證據區塊 case 拿掉 xfail。

### Task 6 — 字數上限改成英文的量

- 檔案：`config.py:266-292`、`mind_ctx.py:52、98-99`、`mind_loop.py:189-190`、`evolution_trigger.py:78`、`tests/_mindloop_factory.py:26-27`
- 改法：「現在的我」80／600 → **250／2000**；self-profile 1200 → **4000**（照 3.3 倍換算，token 成本差不多；要別的數字直接說）。`config.py:290-292` 的「80–600-CJK」註解跟著改。已採納的舊中文「現在的我」不重驗，照常渲染。
- 測試：釘預設值的測試跟著改（`test_config_evolution.py:19-20`、`test_config.py:317`）；傳明確 80／600 的測試不用動。守門測試的 SelfRevision case（`test_private_prompt_language.py:504-520`，`"x" * 90`、`SENTINEL * 250`）是照 80／600 定的大小，改成從 rig 的上限算，「太短」「太長」「違反 persona」「送審」這幾種結果才會繼續出現。改了 factory 預設之後，靠預設值、又用不到 250 字的測試，改成明確傳 80／600。加一條：已採納的 120 字中文「現在的我」在新上限下仍然 `in_sync`、照常渲染。

### Task 7 — `ch:` 框架和剩下的 daemon 中文

- 檔案：`mind_prompt.py:51-54、450、576-592`、`tool_memory.py:124`
- 改法：
  - `[External situation]`：`You're somewhere public and hear a lot that isn't said to you. Not replying is normal here; you speak up when you actually want to say something.`
  - ChannelMessage：主人 `[Owner, DM] Owner: …`／`[Owner, #chan] Owner: …`；陌生人 `[DM] stranger <name>: …`／`[#chan] stranger <name>: …`。MCP：`[AI peer, DM] <name> (another AI, self-described, unverified): …`。
  - 除錯通道那句原本是命令，改成描述：`(This is a debug channel; the peer is checking whether you give a real reply.)`。
  - 兩處死碼直接刪。STALE 拿掉後 `absolute` 參數只管日期格式，留著。
- 測試：Task 1 的 `ch:` case 拿掉 xfail；`test_mind_prompt.py` 等釘舊字串的測試跟著改；刪掉死碼的測試（`test_mind_prompt.py:55-85` 三個 STALE 測試、`test_tool_memory.py:32-43` 和 `:9` 的 import）。

### Task 8 — 收尾

- 過時註解：`current_self.py:9`、`self_profile.py`、`character.py:5`、`persona_guard.py:109`、`memory/fts_store.py:8`、`context_tags.py:84`、`scripts/persona_stability_smoke.py:298`、`tests/test_current_self.py:14`。
- 守門測試沒有 xfail；整個套件對 Task 0 基準全綠。
- `grep -rnP '[\x{4e00}-\x{9fff}]' src/dollos` 剩下的每一處都要能歸進「不在範圍內」：處理中文的程式（含 `voice_eval/wer.py` 的標點）、讀舊檔的相容（`日記`、中文 self-profile 標題）、註解和 docstring（不是工具類別的 docstring：那會變成工具說明）、日誌、給 client 的錯誤訊息、設定檔載入錯誤（`config.py:181、208`）。清單貼給你。
- 照下面「Spec 修正」改 spec。

---

## 驗收

- **測試**：基準加新測試全部通過（原本就失敗的除外）；守門測試的 `allowed` 只剩哨兵，沒有 xfail。
- **server 小量檢查**（Strata，用 `experiments/lang-exp/` 的 capture／run 腳本，`capture.py` 讀這個分支）：先看 `/health` 和 `/metrics`，有人在用就等；`max_tokens` 400、`reasoning_budget_tokens` 256；**總數 ≤ 15 個 request**，結果明確就提早停。
  - 主人說英文「幫我記一件事」×1、主人說中文「幫我記一下…」×1 → `NoteMemory`
  - 主人說中文「幫我盯著這週要做的事…」×1、主人說英文同類的話 ×1 → 議程項目（`OpenLoop`／`PursueGoal`）
  - 日記 request ×2 → `WriteDiary`
  - ReflectionMoment ×2 → `PinSelf`
  - keeper ×1（`run_evolution_pass`，暫存 memory root 放幾筆英文 pin 和日記；keeper + 可能的重試 + skeptic，≤ 3 個 request）
  - 只呼叫別的工具、沒產生要看的內容時可以補跑，補跑也算進 15 個。
- **過關**：日記、筆記、self-profile 條目、議程項目各至少看到一則，**每一則都 0 個漢字**（主人說中文的那兩次也一樣）；keeper 的回覆能被 `parse_keeper_report` 解析（不是 `evo_error`），候選或 NO_CHANGE 理由 0 個漢字。只量、不算過關：有講話的回覆仍然是英文；她 `Recall` 的查詢用什麼語言。
- 不過關就把那則內容和 request 帶回來討論，不加「要用英文寫」的命令。
- 結果補進 `docs/research/2026-10-19-gura-english-speech-results.md`。

## 不在範圍內

- ASR 維持多語（主人可能說中文）。處理中文的程式不動：jieba FTS、斷句器的 `。！？`、`persona_guard`、`name_aliases`（例如 `古拉`）。
- 不轉換、不遷移另一台機器上的舊中文記憶；只保證讀得到（Task 3）。
- **搜尋是比字的**：主人說中文時，自動 `[Memory context]` 的查詢就是他的原話（`mind_loop.py:1142-1148`），找不到英文筆記；舊的中文筆記也對不上英文查詢。FTS 查詢是所有 jieba token 的 OR（`fts_store.py:136`），沒有停用字或詞幹，英文虛詞會帶來雜訊。spec `:521` 已經寫了這個代價；先觀察，要改另寫 plan。
- 給 IPC client 的錯誤訊息（`cascade/tool_loop.py:310-311` 的 `ErrorMsg`）、日誌、trace：不是她的字，也不進 prompt。
- 簡轉繁（opencc）：她不再寫中文，不用做。
- **順便查到、這裡不修**：keeper 的證據區塊讀 `memory_root/consolidated`（`evolution_keeper.py:105`），但 consolidation 寫到 `memory/shared/consolidated`（`kernel.py:561`），所以 keeper 從來沒看到 consolidated。跟語言無關，另外處理。

## Spec 修正

- **redesign spec**（`2026-10-18-dollos-redesign-design.md`）：
  - `:521` Q7 改成「已決定（2026-10-19）：她寫的東西全部英文」，列出這份 plan 改的東西和搜尋的代價。
  - `:378` 改寫：她寫英文，`primary_language = "English"`；pack 不再帶中文原文，重述檢查直接比英文 pack。
  - `:379` 的 `primary_language` 值改成 `"English"`。
  - `:509` 「內在紀錄是中文」那個風險已經不存在，刪掉那半句。
- **演化 spec**（`2026-07-02-slow-self-evolution-design.md`）：`:37` 的「繁體中文」和 80／600 改成英文、250／2000；`:90` 的 `依據` 改 `Evidence`。
- **mind-loop spec**（`2026-10-17-mind-loop-redesign-design.md`）：`:298` 外出紀錄的例子「在一個公開頻道聊了 4 則，心情變成 煩」改成英文（例如 "chatted 4 messages in a public channel; mood became irritated"）：action log 是 daemon 寫的，已經是英文，mood 也是英文字。
- **harness spec**：查過，沒有寫作語言的規則，不用改。
