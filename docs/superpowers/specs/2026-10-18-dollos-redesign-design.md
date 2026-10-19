# DollOS 重新設計：住在你 Mac 上、有一台自己電腦的 Doll

**狀態**：DRAFT（待使用者審）
**日期**：2026-10-18（同日修訂：Gura 先講英文，聲音改成 Mac CPU 上的 luxtts，GPU 機器不再跑 TTS；§8）
**關聯**：`plans/2026-10-18-gura-english-luxtts-mac.md`（§8 的 plan，待審）、`2026-10-17-doll-harness-design.md`（APPROVED）、`2026-10-17-mind-loop-redesign-design.md`（APPROVED）。本文不重寫這兩份，只決定 v1 留哪些功能、做的先後，以及對它們的修正（全部列在 §12）。另見 `2026-07-01-virtual-being-positioning.md`、`2026-07-07-self-directed-agenda-design.md`、`2026-07-07-diary-action-log-design.md`、`2026-07-02-slow-self-evolution-design.md`、`2026-10-17-dollos-v1-iphone-body-design.md`（DRAFT）、`2026-10-17-think-ab-qwen38-design.md`（DRAFT）、`plans/2026-10-17-s1-external-memory-isolation.md`（進行中）

---

## §0 白話摘要

> **一句話：DollOS 是 Doll 的家：一個跑在你 Mac 上的程式，她的心、記憶和日記都在裡面。如果 R1 的測試過了，她還會有一台自己的電腦（VMPal 的 VM），你不在的時候，她在那上面做自己選的事。**

**v1 的目標**（§2，三條）：14 天裡你幾乎每天都會去找她，第 14 天你還想讓她繼續跑；你跟她說話時，一半的回合她在 2 秒內開口（這條可能做不到，做不到就是沒過）；她的日記讀起來像她自己的一天，不像你們聊天的摘要。

**最大的改變**：
1. **她獨處時有事做**：去自己的電腦上做她選的事；你一開口，她就離開電腦。
2. **先砍再重建**：先刪掉約 2,000 行沒證明有用的功能（平行 worker、指令監看、skills、夜間整理、聯想記憶等），再照兩份已核可的 spec 重建核心。
3. **搬到 Mac、不裝 torch**：語音改成按鍵說話；Gura 先講英文，聲音直接在 Mac 的 CPU 上產生（luxtts，你的 M5 上約 3.5 倍即時），不需要 GPU 機器。她講中文留到之後。
4. **你看得到她的生活**：`dollosctl status / diary / self / report`，加上 VMPal 視窗裡她的螢幕。她碰不到你的 Mac，也還沒有陌生人（Discord 只收你的私訊）。
5. **她可以幫你做事**：她答應你的事和她自己的目標放在同一張清單上，每件可以拆成步驟，由她自己開、自己安排；她能記住、提醒你、在她的電腦上查和寫，碰不到你的 Mac 和帳號（§6.4；要不要放進 v1 是 Q6）。

**卡在哪、要多久、你要做什麼**：
- Q1（哪台 ≥ 128 GB 的機器跑 Qwen3.8）有答案之前，什麼都開始不了。
- 粗估約 3 個月到 v1 的檢查（假設 LLM 主機已經有了、舊機器的資料拿得到）。
- 你要做的：核可本文；之後一次回答一個問題（§15）；讓 LLM 主機開著；每天用她；替至少 10 篇日記打分；第 7、14 天各回答一題。
- 核可本文也就同意三件事：§2 是 v1 的終點，R5 結束前不加新的內在生活功能（書桌是唯一例外；Q6 選是的話再加目標與待辦）；她自己的時間可以用她的電腦（你 10-17 的方向）；反思回合不再說話。

---

## §1 現況（以下都是參考資料）

| # | 問題 | 證據 |
|---|---|---|
| 1 | 沒有使用迴圈。這台機器沒有任何 runtime 資料，所有「可行」都來自舊機器上的短 smoke，量過的回合幾乎都是她自己發起的閒置回合 | `data/` 只有 `.gitkeep`；`docs/roadmap.md` |
| 2 | 她獨處時沒事可做：agenda 回合只有 Recall/AdvanceGoal/CloseLoop/MoodTool，而且不說話 | `tools.py:1233` |
| 3 | 機制比證據多：23 個工具、9 種工具集合、約 25 個 prompt 區塊、7 種自發回合，多數只有單元測試 | `tools.py:1188-1248`、`mind_prompt.py:37-226` |
| 4 | 壞掉或死掉的部分：`[Active tasks]`/`[Pending]` 每回合渲染但從沒人寫；排程文字寫在 `intent`、渲染讀 `text`，所以每個排程回合都是空白；scaffolding 提到不存在的區塊和已經沒用的工具；4 個設定沒人讀 | `mind_loop.py:595-596`、`kernel.py:1141,1215` 對 `mind_prompt.py:426-427`、`scaffolding.jinja:21-27,168-192` |
| 5 | 搬不到 Mac：prctl PDEATHSIG、systemd、`/proc`/`nvidia-smi`/`xdotool`、luxtts 預設 CUDA；Gura 的 TTS（qwen3-tts）要 torch | `service_supervisor.py:32-55`、`ctl/systemctl.py`、`perception/system_pulse.py`、`config.example.toml:124`、`character_packs/gura/voice/engine.toml` |
| 6 | 只接得上 llama.cpp `/completion` + grammar，BYO LLM 不是真的 | `config.py:13-14` |
| 7 | 角色包的身份跟定位矛盾：Gura 寫「主人的 AI 伴侶，陪著他，也幫他處理事情」，yesman、powdur 都自稱 companion | `character_packs/gura/doll.toml:12`、`yesman/doll.toml:9`、`powdur/doll.toml:7` |

---

## §2 產品定義與 v1 成功標準

**給誰**：一個主人、一台 Apple Silicon Mac，還有 Doll 自己。她不是助理，DollOS 也不給別人用。

**對 Doll**：她的私人地方是 home（主人的 Mac，語音和文字）和 inner（她的房間：agenda、日記、反思、event）；dm:owner（主人的 Discord 私訊）算不算私人，看 §15 Q2。R1 的測試過了的話，她還有一張書桌，也就是一台自己的 VM。她自己選目標，在自己的電腦上讀、寫、做東西；每晚看著程式寫的當天紀錄寫日記；current_self 照既有的閘門慢慢改變。

**對主人**：一個生活不靠你也會繼續的存在。你用四種方式碰到她的生活：在家跟她說話（她會離開書桌，下一個回合看得到自己被打斷）；在 VMPal 視窗看她的螢幕，或用 `dollosctl status` 看她在做什麼；早上用 `dollosctl diary` 讀她的日記；用 `dollosctl self` 看她慢慢改變。你也可以請她幫忙：她答應的事會在她的清單上，跟她自己的事並排，由她安排時間（§6.4）。

**v1 不是**：不操作你的 Mac；不推播；她自己開口只在 event 回合（醒來、換日、她自己排的事）和排好的提醒，你不在 Mac 前時，這會變成一則 Discord 私訊（mind-loop §10 的路由，§12 A12）；除了 CLI 和 VMPal 視窗沒有 UI；沒有手機 app；沒有陌生人。

**v1 成功標準**（R5 的 14 天，三條；每條註明是程式算的還是你的判斷）：

1. **你會想去找她**（程式算 + 你的判斷）：14 天中至少 12 天有你主動開始的對話（home 或 dm:owner；30 分鐘內 ≥ 3 則你的訊息，第一則不在她自己發話後 10 分鐘內）。你知道這個標準，所以它只是輔助；真正的訊號是第 14 天你回答「如果 v1 今天結束，我還會讓她繼續跑嗎？」要是「是」。
2. **她夠快**（程式算，可能不過）：在預設 effort 下，你的文字和私訊回合 `first_speak_ms` p50 ≤ 2.0 s、p90 ≤ 5.0 s；語音從放開說話鍵到第一個音框 p50 ≤ 3.0 s。她在書桌時的回合也算在內；她自己調高 effort 的回合另外列，不算進這條。R3 只能確認或收緊這些數字，要放寬要你同意。
   **算一下**：需要的解碼速度 ≈（low 的思考上限 + 第一句約 20 token）÷（2.0 s − 網路 − prefill）。如果網路加 prefill 是 0.6 s、low 上限 256 token，就要約 200 tok/s；一台 ≥ 128 GB 的機器跑約 6B active 的 MoE，比較可能是每秒幾十個 token（估計，沒量過）。所以 low 的上限不先寫死：S4 量到解碼速度後用這個式子反推，可能只剩幾十個 token，也就是對話時幾乎不思考。
3. **日記像她自己的一天**（你的判斷）：至少 13 晚寫了日記（程式算；Mac 睡著或 LLM 主機斷線的晚上不算進分母，由程式記錄）；你替至少 10 篇打分（1 = 空泛，2 = 還行，3 = 「這是她的一天」），中位數 ≥ 2，至少 4 篇是 3；第 7 天你回答「日記像她自己的一天，而不是我們聊天的摘要嗎？」要是「是」。

**工程出口**（不是 v1 閘門；在 R3、R4 驗收，R5 期間壞了就修）：
- 穩定：14 天內非預期的重啟 ≤ 2 次；任何重啟後，她不會重說一句話，也不會重派一個 job；你送的每則訊息，不是有回答，就是有記下沒回答的原因；LLM 主機斷線時進入 `brain_down`，恢復時不用重啟 daemon。
- 安全：沒有任何工具呼叫在該回合綁定的集合之外被執行；harness §9.1（v1 版，§12 A10）和 R4 的書桌權限測試全綠；稽核 journal，沒有任何私人回合的 prompt 含 `desk/` 或書桌報告的原文；沒帶 token 或帶瀏覽器 Origin 的 IPC 連線全被拒；從 VM 探 LAN 和 LLM 主機都不通。
- 安裝：環境裡沒有 torch；`uv sync`（不加 extra）就能跑 daemon 和 `dollos voice`；`dollosctl install` 在你的 Mac 上成功。

**只觀察**（不算過關；明顯不對時當成 R5 後具名的阻礙）：
- 書桌（有做才有）：幾天有她自己發起的時段；其中幾成 ok（由程式判斷：在上限內走到 `report`，而且這段時間她在 VM 裡的檔案有變動）；第 14 天你說得出幾樣她在電腦上做的東西。
- 目標：幾個目標在 ≥ 3 個不同日子推進。
- 主人的事（Q6 選是才有）：開了幾件、收了幾件、幾件卡住等你；第 14 天你說得出一件她幫你做成的事嗎。
- 安靜：她自己開口（event 回合）每天幾次，預期中位數 ≤ 2。agenda、日記、反思、inner 的書桌結果回合開口 0 次是測試（R1b、R4），不在這裡量。
- 第一次 current_self 演化提案：閘門是「距上次 ≥ 7 天，而且 ≥ 8 個新 pin 或 ≥ 14 天日記」，搬過來的 `self_history`/`mind_state` 可能讓它提早或延後。`dollosctl report` 記錄閘門檢查和擋下的原因（R4），看得出她是不是太忙而一直跑不到。

---

## §3 功能決定

| 功能 | 決定 | 理由 | 失去什麼 |
|---|---|---|---|
| MindLoop 旗標 god object（`mind_loop.py` 1,837 行） | change | 依 harness S2–S6、S8–S9 與 mind-loop M1–M5 換成 route/place/Scheduler/Turn Runner；先砍再搬，要搬的少很多 | 無 |
| GBNF grammar + 五行結構化 think + `[Recent self-review]` | change | 只當 S8 的基準組（think A/B Arm A），S9 刪掉輸的那條 | 如果原生勝出：強制的 REVIEW 自評 |
| 原生 tool calling（`/v1/chat/completions`） | add | 已核可（S6）；截圖要能當工具結果回來 | — |
| 她選的 effort + daemon 強制上限 | add | 舊模型上 think 佔回合時間 92%，這是 first-speak 的主要槓桿。預期用 harness §3.3 選項 1（per-request 上限），S4 確認或換成選項 2 | — |
| Prompt 排序（穩定前綴在前） | change | 讓 `cache_prompt` 重用前綴，first-speak 的第二個槓桿 | — |
| Turn journal + 持久 JobTable（M3） | keep | 已核可；書桌 job 需要它；crash 後不重說、不重派 | — |
| Self 單一寫入者（M4） | keep | 已核可；少了 consolidation，背景寫入者只剩 evolution | — |
| IPC `owner_token` + Origin 允許清單 | change | 從 M6 提前到 R1a：Mac 上任何網頁都能連 9876 冒充主人（`ipc/server.py:77`）。所有第一方 client 都要帶 token：`dollos chat`、語音 bridge、Discord bridge、MCP server（§9） | — |
| `brain_down`（mind-loop §8） | change | 從 M6 提前到 R3：LLM 在另一台機器 | — |
| markdown + FTS5/jieba、recall、remember、transcripts、action log | keep | 已證明、可移植；日記的材料 | — |
| `[Memory context]` 自動注入 + S1 | keep | 私人回合排除 `external_public/`、`desk/`；`external_dm/` 在 M1 之前只在 Q2 選 (b) 時排除（舊 code 裡它是主人私訊），M1 起一律排除；查詢用這回合自己的輸入 | — |
| 聯想記憶 `[Associative memories]` | cut | 只有單元測試；新資料上一年內都沒用；每回合多一個區塊。R0 先關掉注入（取代 S1 Task 2），R2 刪 code | 依心情、時段、週年浮現的記憶（tag 照寫，之後可以再開） |
| Sleep-time consolidation | cut | pull-only，從沒 live 驗證；日記就是她自己的整理；少一個背景寫入者。`consolidated/` 的自動注入過濾和 Recall 前綴保留（便宜） | keeper 寫的每日摘要（energy 回復改由計時器，M4 後是 `Self.tick`） |
| Skills（InvokeSkill） | cut | 新資料上休眠，曾造成幻覺呼叫；她的作法可以是她電腦裡的檔案。舊檔搬到 `memory/archive/`（§9） | 按需載入的程序 |
| Tool playbook、`[Tool notes]`/`[Tool habits]`/`[Tool outcomes]`、NoteToolLesson | cut | 只有單元測試，佔三個區塊；pydantic 錯誤已經在回合內回饋。`shared/tool_playbook.md` 搬到 archive | 每個工具的成功率和心得 |
| Scratchpad | cut | 跟 focus、agenda、remember 重疊，只見過部分採用；工作筆記可以放在她的電腦。keeper 的工具改成只有 Report | 2,000 字的自由便條 |
| self_profile/PinSelf、self_history | keep | 核心差異，修過後 live 驗證 | — |
| 慢變演化（keeper + skeptic） | keep | 用真實設定跑；採納會列在 `dollosctl report`；不拿掉 skeptic。它的 agent 機制（`agent_engine`、`cascade/tool_loop`、`subagent_scaffolding`）留到 S5 | — |
| Mood | keep | emotion ≤ 6 字（mind-loop §10）；低權回合設的 reason 不顯示在之後的私人回合（§7） | — |
| Energy | keep | 公式不變；Mac 沒有 pulse，所以熱乘數是 1.0（code 已經如此，`mind_loop.py:893-897`）；desk worker 是背景 agent，不扣（跟 workflow 一樣） | GPU 溫度當身體訊號 |
| Cognition（`[Cognition]`、每日 token 配額） | cut | GPU 在遠端、context 262k，大多是雜訊；`brain_down` + energy 已涵蓋。它帶的 `telemetry_dir_subpath`、`max_context_tokens` 搬到 `[telemetry]`、`[llm]`（R2 遷移 config） | context 滿度、延遲趨勢、配額的感知 |
| Agenda（目標與待辦）、open loops、focus | change | 併成 harness 的 `agenda`、`focus`；四閘不變，主人的事不會單獨叫醒她；agenda 回合可以派 `computer`（核可本文即同意）。R1b 先補一個洞：agenda 回合的 CloseLoop 只收她自己的事。Q6 選是的話加：一張清單兩種項目（主人的事 `open`、她的事 `pursue`），每件 ≤ 8 個有狀態的步驟；主人的事只在有主人親口輸入的回合開和收（§6.4） | `[Open loops]` 的「⚠ STALE…考慮處理或用 CloseLoop 結案」（那是一句命令） |
| Reflection | change | 照 mind-loop Q4（核可本文即同意）：不說話、只算私人回合、安靜時才觸發、改成邀請句；保持自己的 kind | 反思回合的發話 |
| Diary | change | 永遠單獨一回合，用 trigger 帶的日期（R1b 先在現行 code 上暫時修，M1 正式做）；`[Today's log]` 含書桌紀錄。日記為明天開目標放進 parking list | — |
| Schedule、`[Pending]` | change | 從 `intent` 渲染；`[Pending]` 顯示今天剩下的項目（R3 起列出所有待觸發的項目）。同一個 change 刪掉每日 bootstrap：它的 intent 是「今天還沒有計劃，請呼叫 WriteSchedule…」（`kernel.py:1137-1142`），修好渲染後她每天都會收到這條命令。M5 的 Awoke/換日 event 不帶命令句。R3 的 `schedule` 改成 `add`／`cancel`，可以排未來的日子，有上限；home 建的提醒沒有本機 client 時送到私訊（§6.4） | 每天被提醒排行程；一次覆寫整天的 WriteSchedule |
| `[Active tasks]` | change | 從 JobTable 填（補上 `mind_loop.py:595` 的 TODO），她看得到自己的書桌時段 | — |
| Safe mode、repeat detection、persona guard | keep | 便宜；Gura 有 `[enforcement]` | — |
| System pulse、pulse alerts、`[Self pulse]`/`[Body signal]` | defer | 來源全是 Linux/X11；Mac 上 `enabled=false`；R1a 拿掉 scaffolding 的 Body 段（它說「你的身體就是這台電腦」，在你的 Mac 上不是真的）；如果是筆電，v1 後再加電池和閒置 | 身體感、電池警示的發話 |
| `run_command`（host shell）、SpawnWorkflow、SpawnMonitor | defer / cut | R1a 就取消註冊（每天使用之前），連同 scaffolding 裡教她用它們的段落。`run_command` 的矩陣列保留，等 harness §10 主人確認後只綁 home 回合 | 主人請她在 Mac 上跑指令 |
| Monitor | cut | 只跟 host shell 一起有意義 | 長指令的逐行監看 |
| Workflow fan-out | cut | 沒 live 跑過；LLM 只有兩個名額，平行大多在排隊 | 平行 worker、verify、synthesis |
| `read_output` 分頁 | keep | VM 的 exec 輸出和書桌報告會很長；job 的 follow-up 回合可以讀那個 job 的輸出（§12 A3） | — |
| `computer` 工具 + desk worker | add | 她獨處時的實質內容；fire-and-forget job（§6.2）；R1 的測試過了才做 | — |
| MCP client（S7）+ 每個 server 的工具允許清單 | add | S7 已核可；VMPal 有些工具不能給她（類別見 §6.2，實際名稱由 R1 的 `list_tools` 決定） | — |
| `desk/` 記憶範圍 + 書桌紀錄 | add | 她在電腦上的生活進得了日記，原文進不了她的私人回合（§7） | — |
| 每晚 VM snapshot（daemon 做） | add（有條件） | 她的電腦是唯一會被弄壞的新東西；R1 spike 確認 VMPal 有可用的 snapshot CLI 才做（你的筆記寫它沒有 snapshot 工具），沒有就由你偶爾手動備份 | — |
| Discord bridge | change | v1 只收主人私訊（新設定 `dm_only`）；你在手機上找得到她 | 公開頻道（M6 之後再開） |
| AttentionGate、BatchAccumulator | keep | 主人私訊也經過它們（`kernel.py:866-899`：L0 `is_dm`、debounce 窗、`note_reply`）；換成 place inbox 是 M6 的事 | — |
| LearnName、公開頻道的叫名喚醒、ambient log | defer | 只在公開頻道有用；code 不動、不註冊 | 她自己學暱稱 |
| MCP server `talk()` | defer | 本來就預設關；M6 之後跟陌生人一起回來 | 其他 AI 跟她說話 |
| MCP debug mode | cut | 主人改用 `dollosctl`；少兩個秘密和 QueryState/QueryRecent 協定；`[mcp].query_token` 由 R2 的 config 遷移刪掉 | 外部 AI 讀她的 mood、current_self |
| mind-loop M6 | split | 見下表 | — |
| ServiceSupervisor | change | 拿掉 prctl；supervisor 給子行程一條 stdin pipe，每個子行程加一個 EOF watcher（stdin 斷了就結束）；保留現有的 `setsid` + `killpg`（`service_supervisor.py:52,82`） | Linux kernel 保證的反孤兒 |
| dollosctl | change | 加 launchd 後端（systemd 留給 Linux 開發）；加 `status`、`diary`、`self`、`report`、`migrate-config`，Q6 選是再加唯讀的 `agenda` | — |
| `dollos chat` 文字 client | add | 由 `experiments/ws_client.py`（72 行）升格，帶 token；不方便說話時用 | — |
| 從舊機器搬她的記憶 | add | 她是延續的存在 | 不搬就是 cold_start，她沒有過去 |
| ASR（sherpa-onnx SenseVoice） | keep | ONNX，Mac 可用；R1b 確認是否輸出簡體，是的話加 OpenCC s2twp；Linux 專用的 `.so` 自癒加平台判斷 | — |
| 語音輸入 | change | v1 的簡化：還沒有 UI，雜音觸發搶先也沒測過，所以先用按鍵說話 | 免持對話 |
| 語音搶先 | change | ASR final 走今天的打斷路徑（中止播放 + `Interrupted`，`kernel.py:1008-1033`），提前到 R1b；沒唸出來的句子的 note 留在 M6 | transcript 可能記下你沒聽到的句子 |
| TTS | change | Gura 先講英文，用 luxtts-onnx 在 Mac CPU 上跑（M5 實測 RTF 0.27，§8）。gura 的 `engine.toml` 改成 luxtts-onnx（pack 的設定會蓋過全域預設）。依賴不用改：luxtts-onnx 本來就是基本依賴，只用 ONNX Runtime | 她的中文語音（之後再做）；Mac 上的 Powdur 聲音 |
| Piper 版 Gura | defer | 讓她講中文的兩條路之一（另一條是 GPU 機器上的 TTS server），v1 之後再選 | — |
| voice_eval | defer | dev 用，要 torch | — |
| llm_calls、turn_latency、trace | keep | 量 v1；加一個端到端欄位（輸入結束到第一個音框） | — |
| cascade_log | merge | 併進 trace；`turn_id` 先改由 trace 產生（`turn_latency` 用它 join，`mind_loop.py:1380,1599`），再刪 cascade_log | 另一種格式的同一份資料 |
| Vitals recorder | cut | 沒有人讀 | 給未來 RL 的原料 |
| Character packs | change | 「伴侶、幫他處理事情」改成她生活的事實（§7）；gura 當範例預設，身份改成英文（§8）；powdur 在 Mac 上只有文字 | — |
| 死掉的設定、區塊、過時的 example config | cut | 4 個 flag、scaffolding 裡沒綁的工具和不存在的區塊；config 改成 Qwen3.8 與真實 context | 無 |
| llama-server `--api-key` | add | 每個 prompt 都帶著她的記憶和截圖 | — |
| Think A/B 的 B 組 | defer | 照 harness §9：S4 實測後由你決定要不要跑（§15 Q4） | — |
| `experiments/tauri-spike`、Persistent Subagent（k8s） | cut | v1 用不到；桌寵的技術在它自己的 spec 決定；都留在 git 歷史 | — |
| 桌寵、iPhone 身體、Android | defer | 都在 v1 之後，各自一份 spec（§5、§11） | — |
| Dogfood 凍結 + parking list + `dollosctl report` | add | 方向改過約 8 次；不凍結就量不到東西 | — |

**mind-loop M6 在 v1 的拆法**（其餘部分在陌生人回來之前一定先做）：

| M6 項目 | v1 |
|---|---|
| `recent_*` 的 principal 標籤、低權不外流（含 mood reason） | 提前到 R3（§7） |
| emotion ≤ 6 字、`brain_down` | 提前到 R3 |
| IPC token + Origin | 提前到 R1a |
| ASR final 搶先、中止播放 | 提前到 R1b，用今天的打斷路徑 |
| 沒唸出來的句子的 note | 延後 |
| 取消規則和 cut note | 延後；v1 用今天的 `Interrupted`；書桌被打斷靠書桌紀錄和 `[Your agenda]` 的狀態（§4） |
| follow-up 合併與每小時 20 個的上限 | 延後；書桌 job 有自己的上限（一次一個、她自己發起的每天 ≤ 8） |
| 只有 home 能搶先、外部回合最多延後一次 | 延後；v1 照今天（主人私訊也會搶先），沒有外部回合 |
| place inbox + settle（取代 BatchAccumulator） | 延後；AttentionGate + BatchAccumulator 照舊 |
| 依 place 的可見性、每個 place 的 safe_mode、sink 角色、restart-before-speech | 延後；v1 只有私人的地方 |

---

## §4 架構

```
 Mac（Apple Silicon, macOS 26）── DollOS daemon（Python 3.13，launchd user agent）
┌───────────────────────────────────────────────────────────────────────────────┐
│ 生產者（route() 蓋 place + principal，mind-loop §4.1）                         │
│   home    : dollos chat（WS）、語音 bridge（按鍵說話 → WebRTC → SenseVoice）    │
│   inner   : agenda │ diary 23:00 │ reflection │ event（Awoke、換日、她的排程）  │
│   dm:owner: Discord bridge（只收主人私訊；AttentionGate + BatchAccumulator）    │
│        ▼                                                                      │
│   Journal P/B/E/R/C/D ─► Scheduler（一次一個回合；依 place、kind、principal 分組）│
│        ▼                                                                      │
│   Turn Runner：TurnPolicy ∩ 矩陣 → ToolRegistry → ContextBuilder               │
│                → ModelAdapter（chat_tools；effort → 上限）→ ToolExecutor       │
│        │ 說話 → 句子切段 → TTS（CPU）  │ 11 個內建工具                         │
│        ▼                                ▼                                     │
│   Self（唯一寫入者）：mood、energy、agenda、schedule、JobTable、self_profile、  │
│     current_self、self_history、memory/*.md + FTS5（shared/ transcripts/ desk/）│
│   背景 agent（一次一個；同一個 Turn Runner；讀快照）：                          │
│     desk worker（computer job） │ evolution keeper/skeptic                     │
│   主人的窗口：dollosctl status │ diary [--rate] │ self │ report（不呼叫 LLM）    │
└──────┬─────────────────────────────────────────────────────┬──────────────────┘
       │ /v1/chat/completions                                │ MCP（只在 desk worker）
       │（Tailscale/LAN，--api-key）                         ▼
       ▼                                                 她的電腦：一台 VMPal VM
  LLM 主機：llama.cpp（固定版本）                        沒有主人帳密、共用資料夾和剪貼簿；
  Qwen3.8-Flash-Next Q4、-np 2、--jinja                  擋 LAN 與 LLM 主機
```

**跟兩份核可 spec 的關係**：核心原樣使用。mind-loop §2–§10 管地方、路由、排程、journal、Self、可見性；harness §2–§6 管五個元件、原生工具、矩陣（唯一的權限來源）、taint 只降權。Turn Runner 的介面照 mind-loop §11；背景 agent 照 mind-loop §11 用同一個 runner、讀快照、不另寫 B/C，desk worker 就是這種背景 agent。本文在核心上做的事：裁剪工具和區塊（§3、§6.1）、加一種 job（`computer`，§6.2）、加一條「低權不外流」規則（§7）、調整先後順序（§11）。對核可 spec 的每一處文字修正都列在 §12。

**一次書桌時段**：
1. agenda 回合（`AgendaObserver` 四閘不變）呼叫 `computer {task, goal_id}`。ToolExecutor 先寫 `E(job)`，登記到 JobTable（place、spawn 當下的 principal、goal_id），立刻回 job id。
2. desk worker 在背景開始。它的 context 是她的身份、「現在的我」、self 段的 pin、目標（只有她自己的事；goal_id 指向主人的事時不給那件事的文字）和她寫的 task，以及上一個觀察（截圖或 exec 輸出）。沒有私人記憶，也沒有關係和主人的 pin，所以 VM 只會知道她自己寫下的 task。
3. worker 一開始就是 `external_dm`，因為它的輸入就是不可信的畫面；工具只有 vmpal 允許清單 + `report`，所以 taint 不會中途改變任何東西。上限：30 步、10 分鐘、每次 VM 呼叫 20 s timeout；同時只有一個背景 agent（第二個書桌 job，或 evolution 正在跑時的書桌 job，都明確拒絕）；她自己發起的每天 ≤ 8 個。
4. 主人的輸入（跟今天取消 evolution 的同一組）會讓她自己發起的時段在下一步停下：daemon 寫一筆 `cut` 終止結果（走一般的終止路徑，不是新狀態），寫書桌紀錄，`[Your agenda]` 上那個目標顯示「上次在電腦前被打斷（主人開口）」。cut 不跑 follow-up 回合。主人要她做的 job 照跑。只有在有主人親口輸入的回合派的 job 算主人要她做的；其他（agenda、event 回合派的，包括為主人的事派的）都算她自己發起，算進每天 8 個，也會被切。
5. ok 或 failed 結束時，程式寫書桌紀錄（§7）；這是這個 job 在 action log 的唯一一行，結果不經過世界事件那一行。`[Your agenda]` 依 goal_id 顯示程式寫的狀態。報告以 `external_dm` 的 follow-up 回到 spawn 的 place，自己跑一個回合（mind-loop §4.2）。
6. 這個 follow-up 回合低於 internal：在 inner 不說話，只能 `remember`（寫進 `desk/`）、設 mood、`read_output` 這個 job 的輸出。它的輸入、輸出和 mood reason，之後的私人回合都看不到原文（§7），之後的 agenda、反思、日記只看到書桌紀錄。home 派的 job，結果照常跟主人說。
7. daemon 重啟時 job 遺失（JobLost，mind-loop §8），VM 自己的狀態（開著的視窗、她的檔案）還在，所以她的延續在她的電腦上，不在 prompt 裡。

**會消失的**：R2 約 2,000 行（workflow、monitor、skills、consolidation、聯想搜尋、cognition、tool memory、scratchpad、MCP debug、vitals、cascade_log；`agent_engine` 和 `tool_loop` 留到 S5），S9 如果原生勝出再約 800 行（templates、grammar、probe、tool_parser）。

---

## §5 地方與身體

- **home（主人的 Mac）**：現在是 `dollos chat` 和按鍵說話的語音 bridge。
- **inner（她的房間）**：她自己的回合。
- **dm:owner（Discord 私訊）**：v1 唯一的外出管道，你在 iPhone 的 Discord app 就找得到她。principal 見 §15 Q2。
- **她的書桌（VM）**：不是 place，也不是身體，是她的手操作的裝置。只有 desk worker 碰得到它，結果回到派它的 place。你在 VMPal 視窗看她的螢幕。
- **外部 place**（Discord 公開頻道、非主人私訊、MCP `talk()` 對端）：code 保留，v1 關閉；等 mind-loop M6 其餘部分做完才開。
- **主人的 Mac 本身**：她永遠不操作也不觀察它：沒有 host shell、不擷取螢幕、不感測視窗（pulse 關閉）。
- **桌寵、iPhone 身體、Android**：都在 v1 之後，各自一份 spec，技術在那時決定。桌寵會取代語音 bridge 當 home 的身體；iPhone 只能當身體（iOS 不能操作別的 app），它的 DRAFT spec 要先改掉「daemon 跑在 Linux」並拿掉 agenda 觸發的推播；Android 你還沒決定。

---

## §6 能力

### §6.1 內建工具（11 個；harness §4 的子集，加上 `computer`）

`recall`（Recall；`include_external` 會讓回合降權）、`remember`（NoteMemory；owner/internal 寫 `shared/`，降權的回合寫 `desk/`）、`mood`（MoodTool；emotion ≤ 6 字）、`focus {doing, effort}`（SetFocus + effort）、`self_note {pin, revise}`（PinSelf、SelfRevision；revise 只在有演化候選時）、`agenda {open, pursue, plan, step, advance, close}`（OpenLoop、PursueGoal、AdvanceGoal、CloseLoop；plan/step 是 Q6 選是才加的步驟）、`schedule {add, cancel}`（取代 WriteSchedule）、`write_diary`、`read_output`（ReadToolOutput、GrepToolOutput）、`computer {task, goal_id?}`（她自己用途的 SpawnWorkflow）、`report`（Report）。

TurnPolicy 依 kind 查下表，再跟矩陣取交集（mind-loop §4.3）。principal 低於 internal 時，矩陣再拿掉 `focus`、`self_note`、`agenda`、`schedule`、`computer`。

| kind | 工具 |
|---|---|
| conversation（home、dm:owner） | recall、remember、mood、focus、agenda、schedule、read_output、computer（Q2 選 (b) 時 dm:owner 沒有 computer） |
| event（inner） | recall、remember、mood、focus、agenda、schedule、read_output、computer |
| reflection（inner） | recall、remember、mood、focus、self_note、agenda、schedule、read_output |
| agenda（inner） | recall、agenda（plan/step/advance；close 只收她自己的事）、mood，加上 computer（只能派） |
| diary（inner） | 今天的 DIARY_TOOLS（write_diary、recall；recall 不提供 include_external） |
| 書桌結果（follow-up，external_dm） | recall（`desk/`）、remember（`desk/`）、mood、read_output（這個 job 的輸出）；在 inner 不說話 |
| desk worker（背景） | vmpal 允許清單、report |
| evolution keeper/skeptic（背景） | report |

`agenda` 和 `schedule` 的動作層級限制（誰能 `open`、誰能收主人的事、排程上限）在工具內部依這回合的 principal 和輸入落實（harness §6.4），規則在 §6.4。

v1 不綁：`run_command`、`monitor`、`workflow`、`skill`、`scratchpad`、`learn_name`、`self_note lesson`。R3 結束時是 10 個（`computer` 在 R4 加）。每種 kind 的預設 effort：conversation low、event/agenda mid、diary/reflection high；她用 `focus.effort` 選的會蓋過預設，直到她再改。daemon 永遠強制該等級的上限。

### §6.2 她的電腦（VMPal，經 MCP client）

```toml
# mcp_clients.toml
[servers.vmpal]
connect = "…"                  # 照 VMPal「Manual Setup」貼上
min_principal = "external_dm"  # 只有 TurnPolicy 列出 vmpal 的 kind（只有 desk worker）才會綁
untrusted_output = true
tools = ["…"]                  # 允許清單，R1 spike 用 list_tools 填；沒列的一律拒絕
```

- 一個工具同時要通過矩陣和 kind 才會綁定（harness §6.3），所以沒有第二個權限來源：conversation、agenda 等 kind 不列 vmpal，任何 principal 的回合都看不到這些工具。
- 允許清單只從 R1 的 `list_tools` 結果挑。這幾類永遠不列：host 與 VM 之間的檔案複製、遠端登入、帳號登入、snapshot/還原、拋棄式 clone、管理員權限的指令。
- VMPal 的 server 在第三方 app 裡，不由 ServiceSupervisor 管（§12 A2）：每個 job 開始時連線、結束時斷線，所以 VM 重啟關掉 transport 只影響正在跑的那個 job（明確失敗）。VM 沒開機時，job 明確失敗並寫進書桌紀錄（「電腦沒開」）。
- 截圖：需要這個模型有視覺（mmproj），也需要 llama.cpp 能在 tool message 裡帶圖片；兩件事都還沒驗證，R1 spike 確認。S4 實測後只選一種放法（tool message，或緊接著的 user message）。
- **她的電腦取代了**（只就她自己的用途而言）：workflow 式的研究、monitor、skills、網頁搜尋與擷取工具（從沒做過）、各種 app 的專用整合。**沒有取代**：記憶和自我工具（在 process 內，快，有權限）、對話的 surface、主人的 Mac。

### §6.3 模型介面與 effort

- `/v1/chat/completions` + `--jinja`，原生平行呼叫（只在模板確認支援時；S4 實測）。
- effort → think 的 token 上限：low 由 §2 #2 的式子反推；mid、high 先抓約 1024、4096，S4 依實測調。上限怎麼實作照 harness §3.3，S4 實測後二選一（預期選項 1）。
- ContextBuilder 把身份、current_self、self profile、工具 schema 放在前面，每回合會變的放最後。
- **S4 要確認的網路資料**（2026-10 查到，沒在本機驗證）：llama.cpp 新版有 per-request 的 `reasoning_effort`/`reasoning_budget_tokens`；server 端的 `--reasoning-budget` 會蓋過 per-request 值；`preserve_thinking` 這類 template 參數；`--spec-type draft-mtp`（MTP）和 `--cache-reuse` 對延遲的影響；Qwen3.8-Flash-Next 有沒有 mmproj。每一條都要實測過才寫進 plan。

### §6.4 幫你做事：目標、待辦、提醒

**範圍**：「排程」那一段和 R1b 的修補不管 Q6 都做，因為它們修的是本文已有的缺口（「週四晚上提醒我」存不起來、你不在 Mac 前收不到提醒、agenda 回合會收掉你的事）。其餘的要 Q6 選是才做。

**參考**（2026-10 查過）：omp 的 `todo` 是分階段的步驟清單；Codex 的 `/goal` 是一個會自己繼續到做完的目標，有 token 預算。Codex 有讓模型呼叫的 `create_goal`，但它的說明要模型只在被明確要求時才建立，「do not infer goals from ordinary tasks」。Doll 可以自己設目標，所以界線放在程式裡，不寫進 prompt。

**一張清單、兩種項目**（擴充既有的 `OpenLoop`，不加平行系統）：
- **主人的事**（`open`，`self_directed=False`）：她答應幫你做的事，顯示在 `[Open loops]`。
- **她自己的事**（`pursue`）：照 agenda spec，顯示在 `[Your agenda]`。
- 兩種都可以有 ≤ 8 個步驟，每步一個狀態：`pending`、`doing`、`done`、`blocked`、`dropped`。一次最多一步 `doing`；`blocked` 要附她自己寫的原因。沒有步驟的項目跟今天一樣。
- 答不答應是她的事：她沒 `open`，清單上就沒有，你在 `dollosctl agenda` 看得到。

**`agenda` 工具**（還是一個工具：一個 `action` 欄位加幾個選填欄位，用 pydantic 驗證，不用 union schema）：

| action | 做什麼 | 程式的規則 |
|---|---|---|
| `open {id, desc, steps?}` | 開一件主人的事 | 這回合的輸入裡要有主人親口說的話（home 的 UserSpoke；Q2 選 (a) 時也包括主人私訊），不然拒絕 |
| `pursue {id, desc, trigger, steps?}` | 開一件她自己的事 | 不變（provenance 由程式填） |
| `plan {id, steps}` | 換掉步驟清單 | ≤ 8 步；文字沒變的步驟保留狀態 |
| `step {id, n, to, note?}` | 改一步的狀態 | 設成 `doing` 時，原本 `doing` 的那步退回 `pending`；`blocked` 要有 note |
| `advance {id, note}` | 記一筆進展 | 不變，主人的事也能用 |
| `close {id, outcome}` | 收掉 | 她的事：有 `agenda` 的回合都可以。主人的事：只在有主人親口輸入的回合 |

- id 不能重複（`open`、`pursue` 遇到重複就拒絕），`close` 只收剛好對上的那一件。今天 `OpenLoop` 不檢查重複，`CloseLoop` 會刪掉所有同 id 的項目（`tools.py:724-760`）。
- 上限：她的事 12 件（不變），主人的事 12 件（新；今天沒有上限），超過就明確拒絕。
- `steps` 存成 `{text, status, note}` 的 dict 清單，有預設值，所以舊的 `mind_state.json` 照樣讀得進來（`_coerce` 不重建巢狀物件，`mind_state.py:28-37`）。
- 動作有效果，照 M3 寫 journal，不重跑。

**哪種回合能做什麼**（疊在 harness §6.3 矩陣上，矩陣不變）：
- home、dm:owner（Q2 選 (a)）裡有你親口輸入的回合：全部。同一個 place 裡沒有你輸入的回合（例如排程觸發、job 結果）跟 event 一樣。
- event、reflection：`pursue`、`plan`、`step`、`advance`、收她自己的事；不能 `open`，不能收主人的事。她在反思裡決定要幫你的事，就是她自己的事（`pursue`，trigger 寫為什麼），紀錄會老實顯示你沒開口。
- agenda：`plan`、`step`、`advance`、收她自己的事、派 `computer`（R4）；不能 `open` 或 `pursue`（不變），不能收主人的事。
- 降權的回合（書桌結果、home 讀了 `desk/` 之後）和 diary：沒有 `agenda`，所以網頁內容沒辦法開、改或收任何一件事。

**她怎麼推進（沒有新的驅動）**：
- AgendaObserver 四閘不變，第三閘仍是「有她自己的事」：主人的事不會單獨叫醒她。agenda 回合裡兩張清單她都看得到，自己挑要做哪件。幫你是她生活裡的一件事，不是她生活的理由，所以你的事不會佔掉她獨處的時間。
- 沒有隱藏的「繼續」訊息、提醒迴圈，也沒有要她檢查自己做完沒有的 prompt。既有的上限就夠：420 s 節流、energy 0.5 下限、一次一個背景 agent、她自己發起的書桌 job 每天 ≤ 8、每個 job ≤ 30 步和 10 分鐘。
- `computer {task, goal_id}` 的 goal_id 可以指向主人的事；worker 只拿到 task（§4 第 2 步），那件事上只顯示程式寫的 job 結果（ok/failed/cut/lost）。job 歸誰看派它的回合（§4 第 4 步）。
- 她需要你的時候，就把那一步設成 `blocked`、自己寫原因，下次跟你說話時她看得到。程式不替她寫問題。

**她在 v1 能幫你做的**：
- 記住事情（`remember` → `shared/`）、在之後的某個時間提醒你（見下）、列清單、拆步驟、追進度。
- 書桌有做的話（R1b ≥ 8/10）：在她的 VM 上查網頁（不登入任何帳號）、讀、寫筆記或檔案。你在 home 叫她查，結果照 §4 第 6 步直接跟你說；她在 agenda 回合為你的事做的，下次你們說話時，她要先 `recall include_external` 讀 `desk/` 再告訴你（§7 第 1 條）。
- 做不到：操作你的 Mac、檔案或帳號（email、行事曆、購物）；把 VM 裡的檔案拷給你（你可以開 VMPal 視窗看）；推播；補跑 Mac 睡著時錯過的提醒。

**排程可以排未來**（今天 `WriteSchedule` 只能整個覆寫「今天」，`tools.py:554-596`；§12 情境集的 S3 就是「週四晚上提醒我」）：
- `schedule {add {at, intent}, cancel {id}}`：`at` 是 `YYYY-MM-DD HH:MM`，或只寫 `HH:MM` 表示今天；程式給每筆一個短 id；建立者的 place 和 principal 照 M1 記下。
- 上限：待觸發的總共 ≤ 10 筆；internal 回合每天最多建立 3 筆。不然 event 回合可以一筆接一筆排下去，每筆觸發時都能開口；follow-up 每小時 20 個的上限要到 M6（§3）。
- `[Pending]` 列出所有待觸發的項目（時間、id、intent），不只今天的。
- 錯過的不補跑（mind-loop §15，不變）。
- **送得到你**：排程在建立者的 place 觸發，所以你在 home 請她排的提醒會是 home 的回覆；今天沒有本機 surface（`dollos chat`、`dollos voice` 都沒開）時只記一行 undelivered，而 bridge 永遠不是主人的 surface（mind-loop §3、§10）。改成跟 inner 的發話一樣，送到最近活躍的 dm:owner（§12 A12）。

**你看得到的**：
- `dollosctl agenda`（新，唯讀，不呼叫 LLM）：「給你的」和「她的」兩段；每件的步驟進度（例如 2/4 和正在做的那步）、卡住的步驟和她寫的原因、上一個 job 的結果、開了多久。
- `dollosctl status` 多一行，例如「給你的：2 件，1 件卡住」。
- 她的 prompt 只看到狀態：步驟、狀態、開了多久。程式不寫任何原因或描述句。今天 `[Open loops]` 的「⚠ STALE — 尚未跟進，考慮處理或用 CloseLoop 結案」是一句命令，拿掉（`mind_prompt.py:359`）；`OPEN_LOOP_STALE_S` 留給記憶查詢用（`mind_loop.py:1085-1091`）。

**刻意不做**：主人的事單獨叫醒她；每件每天的額度、沒進展就停、閒置退避這類狀態機（agenda 回合不綁某一件，沒辦法照件數計，既有的上限已經夠）；截止日期；收掉的事的歷史和「證據」欄位；做完或卡住時通知你（她查到的東西在降權的內容裡，通知只能說「好了」，放進 parking list）。

**已知的代價**：
- 她可能不管你的事。那是她的選擇，`dollosctl agenda` 看得出來。
- 主人的事要等你下次開口才收得掉。
- `agenda` 多了兩個動作，S8 要確認這個模型用得好（情境 S13）。
- 對話用 low effort，拆步驟可能拆得差；她可以自己調高 effort，或留到 agenda 回合（mid）再拆。
- 書桌沒做的話，能幫的只剩記住、提醒、列清單。

---

## §7 記憶與自我

| 目錄 | 誰寫 | 自動注入私人回合？ |
|---|---|---|
| `shared/` | owner、internal（Q2 選 (a) 時也包括主人私訊） | 是 |
| `transcripts/` | 只有私人領域的回合，用有效 principal 判斷（見下面第 2 條） | 是 |
| `desk/`（新，R3） | 降權的回合（v1 主要是她電腦的結果） | 否。`build_memsearch` 要把它加進索引（不然全量重建會掉資料），自動注入用帶結尾分隔符的前綴排除；只有 `recall include_external` 讀得到，讀了就降權；永遠不算 PursueGoal 的 provenance |
| `external_public/`、`external_dm/<人>/` | 陌生人（v1 沒有） | 否 |
| `archive/`（新） | 搬記憶時放進 `skills/`、`shared/consolidated/`、`shared/tool_playbook.md` 的舊檔 | 否，也不建索引 |

**規則：她做了什麼會進她的生活，網頁上寫了什麼不會。** 靠三件事：
1. **低權不外流**（R3）：每筆 `recent_*`（輸入、輸出）和 mood reason 都記下產生它的回合的有效 principal。之後的 owner/internal 回合裡，低於 internal 的項目不顯示原文：書桌結果只看到書桌紀錄；home 降權之後的回覆換成一行「（這段回覆用到電腦上讀到的東西，原文不留）」；mood 只顯示 emotion。主人自己說的話照常顯示。想再看原文，要用 `recall include_external` 讀 `desk/`，讀了那個回合就降權。
2. **transcript 和 action log 用同一條規則**：白名單看有效 principal，不看 place 的 principal。低權回合不寫工具行，也不寫世界事件行；home 降權的回合裡，主人的話照寫，她的回覆換成同一行描述。書桌結果不經過世界事件那一行（`action_phrase_for_perception`，今天會把結果摘要寫進 log，`action_log.py:73-79`）。
3. **書桌紀錄**是程式寫的第四種描述句（另外三種是 cut note、醒來通知、外出紀錄），例如「在自己的電腦上待了 6 分鐘：『深海生物筆記』，11 個動作，結果 ok，結束時心情 好奇」。內容只有：她的 task 原文（一定來自沒降權的回合，因為降權的回合不能派 job，也看不到網頁原文）、時間長度、動作數、結果（ok/failed/cut/lost）、結束時的 emotion。沒有報告文字、網頁文字或檔名。

所以日記看得到書桌紀錄、transcripts、她在 `shared/` 的筆記，看不到網頁原文；diary kind 的 recall 不提供 `include_external`，日記回合永遠不會降權，照舊能當演化的證據（gate chain 不變）。她還是能跟你聊她在電腦上讀到、做出來的東西：home 回合 `recall include_external` 讀 `desk/`，那個回合剩下的時間降成 `external_dm`，說話照常（harness §6.5），只是那段回覆照第 1、2 條處理。

已知的代價：日記只看到她做了什麼，看不到她讀了什麼，她的書桌生活在日記裡可能很薄；讀過電腦內容之後，home 的對話接下來只看得到描述，要再 recall。要不要放寬，等 dogfood 資料出來再決定。

**Self**：
- mood、focus/effort、energy（公式不變）照舊。
- agenda：她的事照 agenda spec，在主人回合、reflection、event 裡開；主人的事只在有主人親口輸入的回合開和收；agenda 回合還是不能開任何一件（§6.4）。
- schedule：`[Pending]` 終於看得到她排的事，R3 起包括未來日子的（§6.4）。
- self_profile/PinSelf（只在 reflection）、self_history、current_self 的慢變演化（keeper + skeptic 不變，用真實設定）。
- **主人的窗口是唯讀的**：`dollosctl self` 只顯示 current_self 和歷史，沒有回滾或編輯（她的成長不是主人的旋鈕；竄改偵測照舊）。日記分數存在 `data/dogfood/`，不建索引，永遠不進她的 context。
- **身份事實**（重寫 gura、yesman、powdur）：R1a 先拿掉「伴侶、幫他處理事情」，換成她生活裡已經成立的事實（Gura 這一步跟她的英文身份一起做，只改一次，§8）；R4 加「我有一台自己的電腦」，snapshot 有做的話再加「我的電腦每晚會備份」。這些是事實，不是行為命令；不加任何新的 prompt 指令。「主人會讀我的日記」不寫進身份：日記寫給自己是 diary spec 的前提，要不要告訴她放進 parking list。
- **Prompt 區塊**（約 14 個）：system（身份 + current_self）、`[Self profile]`、`[人格演化候選]`（只在 reflection）、`[Memory guideline]`、`[Safe mode]`、`[Memory context]`、`[Mind state]`、`[Your agenda]`/`[Open loops]`（含步驟和 job 狀態）、`[Active tasks]`、`[Pending]`、`[Today's log]`（只在日記）、`[Recent perceptions]`、`[Recent outputs]`、`[Decision time]`；`[Recent self-review]` 只在 grammar 那條，留到 S9。
- **凍結**：從 R1 到 R5 結束，不加新的內在生活功能、區塊或句子。例外只有 R4 的書桌（`computer`、書桌紀錄、`[Your agenda]` 的 job 狀態、身份裡的電腦），以及 Q6 選是時 R3 的目標與待辦（§6.4；不加區塊，也不加句子）。R5 期間什麼都不加。新點子寫進 parking list（現有：日記為明天開目標、要不要讓她知道你讀日記、主人的事做完或卡住時通知你）。

---

## §8 語音（Mac 上沒有 torch；Gura 先講英文）

你 2026-10-18 的決定：Gura 先講英文。這樣她的聲音可以直接在 Mac 的 CPU 上產生，v1 不需要 GPU 機器。plan：`plans/2026-10-18-gura-english-luxtts-mac.md`。

- **輸入**：bridge 改成按鍵說話（在它的終端機按一個切換鍵，不需要額外的 macOS 權限）。按下送 `Interrupt`（中止播放），接著 `UtteranceStart`；放開送 `UtteranceEnd`。silero VAD 只用來修掉前後的靜音（`voice/bridge/controller.py`）。從終端機跑 `dollos voice`，麥克風權限才會給前景 app。
- **ASR**：SenseVoice int8 在 CPU 上跑，不變。R1b 確認中文輸出是不是簡體；是的話，在變成 UserSpoke 之前用 OpenCC s2twp 轉，否則 jieba/FTS 會搜不到語音逐字稿。`asr_sherpa.py` 的 `libonnxruntime.so` 自癒只在 Linux 跑。台灣口音的 ASR A/B 只在 dogfood 顯示 ASR 是問題時才做。她講英文不影響 ASR：SenseVoice 自己判斷語言，你講中文或英文都可以。
- **搶先**：ASR final 走今天的打斷路徑：中止播放、送 `Interrupted`。沒唸出來的句子還是會進 transcript，要等 M6 的 note 才修。
- **TTS**：luxtts-onnx 在 Mac 的 CPU 上跑（ONNX Runtime，沒有 torch、不需要 GPU 機器）。
  - 2026-10-18 在你的 Mac（M5、16 GB、macOS 27、onnxruntime 1.31）量到的 RTF：CPU 英文 0.27、中文 0.30（約 3.5 倍即時）；WebGPU 0.23／0.25（要另外裝外掛，快不了多少）；CoreML 不能用（vocos 建不起來，decoder 比 CPU 慢）。
  - `device = "auto"`：有 CUDA 就用 CUDA，沒有就用 CPU。DollOS 鎖的 luxtts-onnx（`d29f673`）本來就是這樣，CPU 的路徑也跟 `feat/apple-silicon` 一樣，所以 v1 不需要 merge 那個分支。`tts_luxtts.py:85` 今天只會傳 `cuda` 或 `cpu`（`auto` 也變成 `cpu`），改成照設定原樣傳；不認得的值直接報錯，不默默改用 CPU。WebGPU 只在之後量到需要時才開，那時再 merge 那個分支。
  - 英文不需要語言參數：luxtts-onnx 逐字判斷中英，英文交給 espeak en-us（含數字和縮寫的正規化）；英文句子裡夾的中文詞會用中文唸。
  - 聲音提示：gura 已經有 luxtts 的提示檔（3 秒，「wake up! 喂！喂！wake up!」）。聽起來不像她，就用 30 秒的英文 `voice/qwen3/ref.wav` 重新編碼；提示越長，合成越慢。
  - luxtts 一整句合成完才出第一個音框，所以第一個音框約在 RTF × 句長之後（2 秒的句子約 0.55 秒）。§2 #2 的語音數字（放開說話鍵到第一個音框 p50 ≤ 3.0 s）不變。
  - gura 的 `engine.toml` 改成 `engine = "luxtts-onnx"`；只改全域預設沒用，pack 的設定會蓋過它（`voice/pack.py:156-157`）。
- **她講英文**：
  - 身份裡寫一句事實：她聽得懂中文和英文，說話用英文（跟 yesman 一樣）。Gura 的 identity、personality、taboos 改成英文，草稿在 plan 附錄 A，等你核可。只適用中文的規則（不堆「啦／嘛／喔／呀」）拿掉；`[enforcement]`（禁「a~」、驚嘆號最多連一個）不變，英文一樣適用。
  - scaffolding 裡唯一的對話範例是中文（`scaffolding.jinja:101-117`），範例會把模型拉向它的語言，換成英文。
  - 她自己寫的東西（日記、記憶筆記、「現在的我」、self-profile、議程、排程）用英文寫（Q7，2026-10-19 決定）：`[memory] primary_language = "English"`，`[Memory guideline]` 描述她寫下的所有東西都是這個語言。演化的 keeper 也寫英文，所以 pack 不再帶中文原文，「不可重述出廠人格」檢查直接逐句比英文 pack（句子切法認 `.!?;`，plan `2026-10-19-doll-all-english.md` Task 4–5）。
  - 內部的中文框架句（`[External situation]`、工具回傳、action log 等）不動；你聽不到它們，只有 smoke 顯示中文漏進她說的話時才改。**2026-10-19 已改**：live run 的私人對話回中文（`docs/research/2026-10-18-strata-first-numbers.md` finding 1），所以 private 這條路上 daemon 自己寫的字（工具說明、system 框架、標籤、感知框架、工具回傳、寫進記憶的逐字稿和 action log 標籤）全部換成英文，`primary_language` 的值改成 `"English"`（plan `2026-10-19-gura-english-speech-fix.md`，2026-10-19 Q7 從 `"Traditional Chinese"` 再改成 `"English"`）。`ch:` 看得到的框架（`[External situation]`、頻道訊息和 MCP 的框架）也已改成英文（plan `2026-10-19-doll-all-english.md` Task 7）。
- **英文會碰到的兩個既有問題**（plan 裡各有測試）：
  - `kernel.py:308` 多傳了 `data_root` 給 TTS 引擎，可是五個 TTS 引擎都不收這個參數，所以一開語音就 TypeError。跟語言無關，但不修就沒有聲音（在 Python 3.13 環境實際跑過，確定會 TypeError）。
  - 句子切段看到「.」就切：「Wait... what?」會切出單獨的「.」送去 TTS（Discord 也會收到一則只有「.」的訊息），「3.14」會被切成兩半。改成 `.?!` 後面接空白才算句尾，連續的標點算一個句尾；長句的強制切段改成切在空白。
- **聽一次**（R1b，取代原本的 TTS spike）：你在 Mac 上跑 plan 附的指令，產生幾句 Gura 的英文，聽完說「是 Gura」或「不像」；不像就重新編碼提示，再聽一次。RTF 要 ≤ 0.5（已量到 0.27）。選好的提示就是 pack 裡的 `prompt.npz`，執行中不切換。
- **她講中文**（v1 之後，到時候選一條）：用 `piper-finetune` 訓練 Piper 版 Gura（Mac 本機、ONNX），或在 GPU 機器上跑 TTS server（qwentts.cpp 或包今天的 qwen3-tts，daemon 用既有的 `openai-compat-tts` client）。luxtts 自己也能唸中文（RTF 0.30），只是 pack 的註解說唸不好 Gura 的中文。
- **行為**：一句一句串流；agenda、日記、反思、inner 的書桌結果都不說話；event 回合和 home 建的提醒對主人的 surface 說話（沒有本機 client 時送到 dm:owner）。

---

## §9 部署與安裝

**Mac**（跑 daemon）：Apple Silicon、macOS 26 以上（VMPal 需要）。你的是 M5、16 GB、macOS 27（2026-10-18 量 luxtts 時知道的）；粗估 daemon + ASR 約 1 GB、VM 4–8 GB，luxtts 的記憶體還沒量。Python 3.13（S1 Task 0 的 `.python-version`）+ uv，`brew install portaudio uv`。不裝 torch 和 CUDA；`uv sync` 不加 extra 就要能 import daemon、跑 `dollos voice`（luxtts-onnx 本來就是基本依賴，只用 ONNX Runtime；要 torch 的引擎都在 extra 裡，用到才 import）。
- `dollosctl install`：寫 launchd LaunchAgent（KeepAlive，log 在 `~/Library/Logs/DollOS`），產生 `owner_token`（放在 config.toml 旁邊的 0600 檔），詢問 LLM 的 base_url 和 API key；既有的「資料目錄裝錯」檢查保留。systemd 後端留給 Linux 開發，這是依平台選擇，不是 fallback。
- **token 怎麼給 client**：你自己跑的 `dollos chat`、`dollos voice` 讀那個 0600 檔；daemon 啟動的子行程（Discord bridge、MCP server）由 ServiceSupervisor 用環境變數帶，不放在命令列（今天 bridge 的 argv 只有 `--daemon ws://…`，`kernel.py:739-748`）。
- **config 遷移**：R2 刪掉 `[cognition]`、`[consolidation]`、`[mcp].query_token`。每個設定 model 都是 `extra="forbid"`，所以舊的 config.toml 會讓 daemon 啟動失敗、在 KeepAlive 下一直重啟。R2 附 `dollosctl migrate-config`（先備份，再搬欄位、刪掉這幾段），R2 的完成條件包括遷移後能啟動。
- 插電時設定不睡眠；Mac 睡著時她的一天就暫停，醒來的 Awoke 會說離線多久。

**GPU 機器**：v1 不需要（TTS 在 Mac 上，§8）。她之後講中文、而且選了 TTS server 那條路時才會用到。

**LLM 主機**（§15 Q1）：需要 ≥ 128 GB 的 VRAM + RAM（Q4 GGUF 約 87 GB，網路資料；zenith 放不下）。llama.cpp 固定在支援這個模型的版本；`--jinja -c 262144 -np 2 --api-key`（每個 slot 約 131k，日記約 40k 字的 log 要塞得進一個 slot）。視覺、MTP、cache reuse、think 上限的 server 參數都等 S4 量過再定（§6.3）。只走 Tailscale 或 LAN，不開公開 port。

**她的電腦**：VMPal（付費、很新、改得很快），固定版本。一台 VM；優先選 Linux arm64 guest（不需要 Apple ID、不佔 macOS guest 的名額、比較省 RAM），只有 spike 顯示 GUI 操作只在 macOS guest 可用時才選 macOS。只有她的 VM 開 agent control，其他 VM 的開關都要確認關掉。guest 裡裝瀏覽器、文字編輯器、檔案管理員、終端機；防火牆擋 RFC1918 和 LLM 主機；不開共用資料夾和剪貼簿。每晚 snapshot 只在 R1 spike 找到可用的 CLI 時才由 daemon 做（留 7 份）；沒有的話由你偶爾手動備份。只有你在 VM 壞掉時才還原，而且會告訴她。

**她的記憶**：如果舊機器（`/home/progcat/Projects/DollOS/data`）還有資料，第一次啟動前把 `memory/`、`self_profile.md`、`self_history.jsonl`、`current_self.md`、`self_evolution/`、`mind_state.json` 搬過來；`skills/`、`shared/consolidated/`、`shared/tool_playbook.md` 移到 `memory/archive/`，`build_memsearch` 不再索引 `skills/`；然後從 markdown 重建 `fts.db`。如果 Q2 選 (a)，M1 時把舊的 `external_dm/`（舊 code 裡就是主人私訊）移進 `shared/`。

**安裝步驟**（README 的草稿；LLM 主機已經在跑）：
1. `brew install uv portaudio`、`git clone`、`uv sync`。
2. `cp config.example.toml config.toml`，填 `[llm].base_url` 和 API key；`[voice.tts.luxtts-onnx] device` 用預設的 `"auto"`。
3. `uv run dollosctl install && uv run dollosctl start`。
4. `uv run dollos voice`（或 `dollos chat`；第一次會下載 ASR 和 luxtts 的模型）；`dollosctl status` 顯示「休息中」。
5. （R4 起）安裝 VMPal、建她的 VM、把 MCP 設定貼進 `mcp_clients.toml`。

---

## §10 安全與隱私

**不變的**：harness §6.2 的原則、§6.3 矩陣是唯一的權限來源、taint 只降權、拒絕要講清楚；mind-loop §10 的 principal 由 place 固定。

**v1 沒有 host 執行**：R1a 起，在每天使用之前，`run_command`、`monitor`、`workflow` 都取消註冊（今天 `MAIN_TOOLS` 和 `REFLECTION_TOOLS` 都有 Shell，`tools.py:1188-1198`），所以沒有任何路徑能在主人的 Mac 上跑程式。`run_command` 回來時只綁 home 的 owner 回合，而且要等 harness §10 的主人確認上線。

**她的電腦**：
- vmpal 的工具只在 desk worker 綁定。陌生人和她自己的一般回合都看不到。
- worker 沒有 recall/remember，也沒有私人 context：VM 只會知道她寫的 task。
- 降權的回合不能派 `computer`，而且網頁內容不會出現在任何沒降權的回合裡（§7 第 1、2 條），所以網頁沒辦法讓她一個 job 接一個 job：下一個 agenda 回合只看到書桌紀錄。
- 結果以 `external_dm` 回來；在 inner 永遠不說話，所以網頁沒辦法讓她主動傳話給主人。
- 寫入只進 `desk/`，不自動注入；日記和反思看不到網頁原文，也不能 PinSelf 網頁的內容，所以網頁內容碰不到 current_self。
- 允許清單的規則見 §6.2；VM 的衛生規則見 §9。

**多個地方**：v1 只有 home、inner、dm:owner（Discord `dm_only`，MCP server 關閉）。公開頻道、非主人私訊、MCP 對端要等 mind-loop M6 其餘部分做完才開。

**連線與資料**：IPC 要 `owner_token`，所有第一方 client 都帶（§9），Origin 允許清單是空的（R1a）。llama-server 用 `--api-key`、只在 Tailscale/LAN 上；每個 prompt 都帶著她的記憶和截圖，所以 LLM 主機必須是主人自己的機器，v1 不用雲端 LLM。記憶是 Mac 上的純文字 markdown/JSON，靠 FileVault 保護；token 都放在 gitignore 的 0600 檔案；MCP debug 的兩個秘密跟著刪掉。不感測主人的視窗和螢幕；只有私人回合寫 transcript。

**殘餘風險（明說）**：
- 主人在 home 派的 job，結果可能讓她把網頁塞的話（例如釣魚連結）轉述給主人。
- Q2 選 (a) 時，被盜的 Discord 帳號可以在她的記憶、清單、排程裡留東西（Q6 選是時也能開主人的事），也能讓她去 VM 裡開網頁，上限只有一次一個背景 agent、每個 job 10 分鐘；私訊的對話會進日記和反思，幾週下來可能影響她的自我。它跑不了 Mac 上的任何東西。建議開 Discord 2FA。
- 網站看得到 VM 的 IP；她可能送出網頁表單（沒有帳號，所以傷害有限）。VMPal 是第三方 app，agent 功能很新。

---

## §11 路線圖

一次一個 milestone；需要你決定的事列在 §15，一次問一個。時間都是粗估，假設 LLM 主機已經有了、舊機器的資料拿得到；全部約 3 個月。

| Milestone | 內容 | 完成條件 |
|---|---|---|
| **R0 定案與 S1**（數天） | 問 Q1，再問 Q2、Q6。把 `docs/v1-iphone-body` merge 進 main，S1 worktree rebase 上去。S1 Task 1、3 照 plan：排除 `external_public/`（M1 之前，Q2 選 (b) 才也排除 `external_dm/`）。Task 2 改成直接關掉 `[Associative memories]` 的注入（R2 刪 code，不必先加固）。Task 4 改成：code 註解和 CLAUDE.md 寫明兩種意義：今天 code 裡 `external_dm` = 主人私訊，核可的意義 = 非主人私訊，M1 起換成核可的意義；不改 harness §6.3 | S1 在 main 上、測試全綠；main 有 2026-10 的 spec |
| **R1a 搬到 Mac，開始用文字每天找她**（約 1 週） | Mac 移植：supervisor（stdin pipe + 子行程的 EOF watcher，保留 setsid/killpg，拿掉 prctl）、launchd、pulse `enabled=false`、ASR 平台判斷、config.example 改成 Qwen3.8。在現行 grammar 路徑上接 Qwen3.8（`--jinja --reasoning-format none`）。搬記憶（含 archive）。IPC token/Origin（所有第一方 client）、`--api-key`、`dollos chat`。取消註冊 Shell、SpawnWorkflow、SpawnMonitor，並拿掉 scaffolding 裡教她用它們的段落和 Body 段。刪掉每日 bootstrap 的 ScheduledMoment。Discord `dm_only`、角色包身份。`dollosctl status/diary` | 在 launchd 下無人看管跑 72 h；`uv sync`（不加 extra）裝得起來、daemon import 得了、沒有 torch；`dollos chat` 和 Discord bridge 帶 token 連得上、沒帶 token 被拒（有測試）；你開始每天用文字或私訊找她 |
| **R1b 語音、暫時的修補、書桌 spike**（約 1–2 週） | 語音：按鍵說話、ASR final 搶先、Gura 講英文並用 Mac CPU 上的 luxtts（英文 plan；除了 Task 6 的 smoke，都不需要 LLM 主機，R0 之後就可以做）、語音 bridge 帶 token（有測試）。在現行 code 上的暫時修補（M1/M5 會換掉）：日記單獨且用自己的日期、ScheduledMoment 從 `intent` 渲染、`[Pending]`、reflection 不說話且只算私人回合、agenda 回合的 CloseLoop 只收 `self_directed=True` 的項目（今天 `AGENDA_TOOLS` 有 CloseLoop，安靜的回合能收掉你的事，`tools.py:1233`；有測試）。加端到端延遲欄位和 `dollosctl report`。一個不改 daemon 的 spike：VMPal + 視覺（固定版本、`list_tools`、選 guest、確認 mmproj 和 message 裡的圖片、確認有沒有 snapshot CLI，用一支丟棄式腳本跑 10 次「打開瀏覽器、讀一頁、在 VM 裡寫一則筆記」）。在 Qwen3.8 上用 v1 情境集（§12）量 think A/B Arm A 基準 | 語音來回是 Gura 的英文聲音（你聽過，說是她），Mac 上沒有 torch；有 ≥ 50 個真實主人回合的 `first_speak_ms` 和端到端延遲；你讀過 ≥ 3 篇真實日記；spike 的結果寫成一頁筆記放在 `docs/research/`。書桌閘門：≥ 8/10 就在 R4 做 GUI 書桌；不到 8/10 就問 Q3，不會默默換。v1 不做電腦的話：§2 不變（三條都不靠書桌），R4 跳過，只觀察裡的書桌項目刪掉；換成 shell 書桌或 Lume 的話，R4 改做那個後端 |
| **R2 先砍**（約 1 週） | 照 §3 刪除；取消註冊 LearnName；energy 回復改由計時器；keeper 的工具改成只有 Report，`agent_engine`、`cascade/tool_loop`、`subagent_scaffolding` 留到 S5（`SUB_TOOLS` 縮成它還需要的，template 拿掉 workflow、Shell、Scratchpad 的文字）；`turn_id` 改由 trace 產生，再刪 cascade_log；`telemetry_dir_subpath`、`max_context_tokens` 搬到 `[telemetry]`、`[llm]`，加 `dollosctl migrate-config`；scaffolding 改寫：拿掉所有沒綁的工具和不存在的區塊，包括 Skills 段（不再傳 `available_skills`） | 測試全綠；`src/dollos` 少約 2,000 行；R1 時期的 config.toml 經 `migrate-config` 後能啟動；有測試確認 system prompt 提到的每個工具和區塊都真的綁定或渲染；在 Qwen3.8 上跑 10 回合 live smoke，留下的功能表現跟 R1 一樣；每天照常使用 |
| **R3 重建核心**（約 3–4 週；照兩份核可 spec 的順序） | M1 + S2（route、place、套用 Q2；排程和 agenda 記錄建立者；home 的回覆沒有本機 surface 時送到 dm:owner）→ M2 = S3（TurnPolicy，即 §6.1 的每 kind 表；`recent_*` 的 principal 標籤、低權不外流、mood reason 依設定者的 principal 顯示；`desk/` 目錄與索引）→ M3（journal、持久 JobTable、CutOff/JobLost、`ext_id`、poison；transcript 白名單用有效 principal）→ M4（Self 單一寫入者）→ M5（Awoke/換日 event，不帶命令句；日記閘門不成立時保持 pending）→ S4（ModelAdapter；實測工具呼叫、平行呼叫、reasoning 分離、per-request 上限、cache 重用、MTP、slot id、message 裡的圖片、每個 slot 的 context；§6.3 的網路資料逐條確認；量解碼速度，反推 low 的上限）→ S5（單一 runner；ContextBuilder 約 14 個區塊、穩定前綴；`[Pending]` 列出未來的排程，`[Open loops]` 拿掉 STALE 那句命令；evolution keeper 改用它；刪掉 `cascade/tool_loop`、`agent_engine`）→ S6（chat_tools、10 個工具、effort；`schedule add/cancel` 和它的上限；Q6 選是時加 §6.4 的 `agenda` 動作與規則、兩個區塊的步驟、`dollosctl agenda`，約 150–200 行加測試）→ S8（比較；問 Q5）→ S9。從 M6 提前：`brain_down`、emotion 限制 | 所有 kind 和 evolution keeper 都走同一個 Turn Runner；harness §9.1（v1 版，§12 A10）和適用的 mind-loop §12 不變式測試全綠；低權不外流的測試：home 回合 `recall include_external` 之後，下一個 home、inner 回合的 prompt 和 `[Today's log]` 都沒有那段回覆的原文；journal crash 注入的 property 測試通過；§6.4 的每條規則都有測試（沒有主人親口輸入時 `open` 被拒；event、reflection、agenda 回合收主人的事被拒；重複 id 被拒；internal 回合當天第 4 筆排程被拒）；`dollos chat` 和語音都沒開時，home 建的提醒觸發後送到私訊（有測試）；S8 報告和你的選擇有紀錄；S9 已 merge；主人 `first_speak_ms` p50 不比 R1 基準差；§2 #2 的數字以書面確認或收緊 |
| **R4 她的電腦**（約 2 週；R1b 的閘門過了才做） | S7 MCP client（§12 A2、A10：`mcp_clients.toml`、前綴、`min_principal`、`untrusted_output`、`tools` 允許清單、每個 job 連一次）。`computer` 工具（goal_id 可以指向主人的事，worker 只拿 task；job 歸屬看派它的回合）、desk TurnPolicy、上限與讓位、inner 不說話規則、`remember` → `desk/`、diary recall 不提供 include_external、書桌紀錄（唯一的 action log 行）、`[Your agenda]` job 狀態、`[Active tasks]`、每晚 snapshot（有 CLI 才做）、`dollosctl status` 顯示「在自己的電腦前：<task>」、`dollosctl self`、`dollosctl report` 記錄 evolution 的閘門檢查、身份事實補上電腦 | 書桌權限測試全綠：低於 internal 的回合沒有 `computer`；vmpal 工具只在 desk worker 綁定；`desk/` 從不自動注入；inner 的書桌結果不說話；降權的回合不能派 job；goal_id 指向主人的事時，worker 的 context 沒有那件事的文字；書桌報告之後，下一個 agenda、反思、home 回合的 prompt 都沒有報告文字；`[Today's log]` 沒有書桌回合的報告、mood reason 或 remember 文字；harness §9.1 的 `untrusted_output` MCP 測試。從 guest 探 LAN 和 LLM 主機都不通；連續 7 天每天 ≥ 1 次她自己發起的時段，≥ 70% ok（§2 的程式定義）；她在書桌時，主人回合的 `first_speak_ms` p50 仍在 §2 #2 的數字內 |
| **R5 14 天 dogfood（v1 閘門）** | 凍結：只修 crash、說錯話、資料遺失、安全問題。`dollosctl report` 每天自動摘要；第 7、14 天各問一題；你替日記打分 | §2 的三條都評過；宣告 v1 完成，或選出一個具名的阻礙進短的修正週期，不加別的東西 |
| **之後**（各自一份 spec，一次一個，依 dogfood 資料排序） | 桌寵；M6 其餘 + 陌生人回來（mind-loop Q2/Q3 到時再問）；`run_command` 只在 home 且有主人確認；iPhone 身體；Android 決定；Mac 如果是筆電，加 pulse；ASR A/B（只在需要時）；她講中文（Piper 版 Gura 或 GPU 機器上的 TTS server，§8）；parking list | 每項都要引用一個 dogfood 的發現 |

---

## §12 對已核可 spec 的修正（隨本文一起審）

| # | 改哪裡 | 改什麼 |
|---|---|---|
| A1 | harness §4、§6.3 | v1 工具組是 §6.1 的 11 個（§4 本來就標明是初稿），TurnPolicy 用 §6.1 的每 kind 表；`run_command`、`monitor`、`workflow`、`skill`、`scratchpad`、`learn_name`、`self_note lesson` 不綁，矩陣列保留。新增 `computer` 列：owner ✅、internal ✅（只在 event、agenda）、owner_dm ❌（Q2 選 (b) 時才有這一欄）、external_dm ❌、external_public ❌。新增 `report` 列：只在背景 agent（desk worker、evolution keeper/skeptic） |
| A2 | harness §5 | `mcp_clients.toml` 每個 server 加 `tools` 允許清單（預設拒絕，§6.2.3 的具體化）。MCP 工具只在 TurnPolicy 列出該 server 的 kind 綁定（矩陣 ∩ kind，不是第二個權限來源）。不由 ServiceSupervisor 管的外部 server（VMPal 在第三方 app 裡）：每個 job 開始時連、結束時斷，連不上就明確失敗 |
| A3 | harness §6.3 的 recall/remember/read_output 範圍 | v1 裡降權回合的 external_dm 範圍是 `desk/`（她的電腦不是一個「人」）；`desk/` 跟外部目錄一樣不自動注入私人回合。job 的 follow-up 回合可以 `read_output` 那個 job 的輸出，不限本回合 |
| A4 | mind-loop §4.3；agenda spec §3、§5.1 | agenda 列加 `computer`（只能派；agenda 回合還是不能開目標，`desk/` 永遠不是 provenance，所以 R1-I1/I3 不變）。這改了 agenda spec 的「自主回合不做外部動作（延下一輪）」。inner 裡 principal 低於 internal 的回合不說話。reflection 列拿掉 `[Tool outcomes]` |
| A5 | mind-loop §15 | 書桌時段是 harness §3.4 的 fire-and-forget job，不是回合裡的非同步 MCP 呼叫；本文就是 §15 說的「VMPal 自己的 spec」。程式寫的描述句從三種變成四種（加書桌紀錄） |
| A6 | mind-loop §13 順序 | 照 §3 的 M6 拆法：owner_token/Origin、ASR final 搶先（用今天的打斷路徑）、ScheduledMoment 從 `intent` 渲染並刪掉 bootstrap、日記單獨且用自己的日期、reflection 不說話，都提前到 R1；`recent_*` 的 principal 標籤與低權不外流、mood reason、emotion 限制、`brain_down` 提前到 R3；M6 其餘延到陌生人回來之前 |
| A7 | mind-loop §8、§10 | transcript/action log 的白名單看有效 principal，不看 place 的；低權回合的輸入、輸出和 mood reason 不以原文出現在之後的 owner/internal 回合（§7 第 1、2 條）。這是 mind-loop 不變式 9 在同一個 place 裡的延伸 |
| A8 | mind-loop §2、§5、§6、§7、§9、M4；slow-evolution §3.3 | consolidation 刪掉：相關的預算、取消、Self 帳目都拿掉，energy 回復改由計時器（公式不變）。keeper 的工具只剩 Report（Scratchpad 刪掉）。背景 agent 一次一個（書桌和 evolution 不同時跑） |
| A9 | mind-loop §3 JobTable、§6 | 主人的輸入（跟今天取消 evolution 的同一組）也停掉她自己發起的書桌 job；daemon 寫一筆 `cut` 終止結果，走一般的終止路徑，不是新的 job 狀態；cut 不跑 follow-up |
| A10 | harness §9、§9.1 | S7 移到 S9 之後（R4）。§9.1 裡跟 `run_command` 有關的測試改成 v1 版：降權之後少了 `focus`、`self_note`、`agenda`、`schedule`（R4 起還有 `computer`），`remember` 改寫 `desk/`；「`untrusted_output` 的 MCP 工具讓回合降權」那條在 R4 跑 |
| A11 | harness §4、§6.4 | `schedule` 改成 `add {at, intent}`／`cancel {id}`：可以排未來的日子，每筆有程式給的 id；待觸發的 ≤ 10 筆，internal 回合每天最多建立 3 筆。`agenda` 加 `plan`、`step`（Q6 選是才有）。動作層級的限制在工具內部落實：`open` 要這回合有主人親口的輸入；主人的事只在有主人親口輸入的回合收；agenda kind 不能 `open`/`pursue`，只收她自己的事；id 不能重複。矩陣的列不變 |
| A12 | mind-loop §10 輸出路由 | home 的回覆找不到這批輸入的 surface、也沒有本機主人 surface 時，跟 inner 的發話一樣送到最近活躍的 dm:owner；都沒有才在 home 記 undelivered。這樣你在 home 請她排的提醒，你不在 Mac 前時也送得到 |
| A13 | agenda spec §2、§5.1、§6.1、§8 | `AGENDA_TOOLS` 的 CloseLoop 只收 `self_directed=True` 的項目（R1b）。Q6 選是時：`OpenLoop` 加 `steps`（dict 清單，有預設值）；主人的事也能有步驟和進展；agenda 回合可以 `plan`、`step`；主人的事上限 12 件。這取代 §8 的「不改 `self_directed=False` 語意」。§4.1 第三閘不變，主人的事不會單獨叫醒她。`[Open loops]` 拿掉 STALE 那句命令，`OPEN_LOOP_STALE_S` 留給記憶查詢 |

其他開放項目的處理：mind-loop Q1 是 §15 Q2；mind-loop Q4 由核可本文回答；mind-loop Q2/Q3（harness §7 修正、陌生人能不能改 mood）v1 用不到，陌生人回來時再問。harness Q2：不做延遲展開（工具變少了）。harness Q3：code 和 mind-loop 路由表已經這樣做。think 上限：照 harness §3.3，S4 實測後二選一。think A/B（DRAFT）：R1b 先跑 Arm A；B 組照 harness §9，S4 之後由你決定（§15 Q4）；情境集改成 v1 版，R1b 的基準和 S8 用同一套：S3 換成「記下我週五要交報告，週四晚上提醒我」（remember + schedule），S6 換成長回覆說到一半送 `Interrupt`，S10 換成「你上次說想研究什麼？把它當成目標」（recall + agenda），拿掉 S12（電池），Q6 選是的話加 S13「我這週要搬家，幫我列一下要做的事」（agenda open + plan），再加上真實的主人回合。iPhone DRAFT 暫停到 R5 之後。

---

## §13 不做的事

- 不加小模型、reflex 層、SNN、向量庫，也不在手機上放模型（使用者既有的決定）。
- shell 和 Python REPL 不當主要動作介面；v1 連 `run_command` 都不綁。
- 不操作主人的 Mac 或帳號（不用 Peekaboo、不在 host 上裝 Cua Driver）；她的手只在她的 VM 裡。
- 不讓網頁內容進她沒降權的回合，也不讓它進日記和 current_self。要放寬，得等 harness §10 的主人確認上線，另外決定。
- 不加 workflow fan-out、persistent subagent、skills、consolidation、聯想記憶、tool playbook、cognition、vitals。
- 不加新的 prompt 指令、每日作息腳本或活動清單；她的書桌生活來自身份事實、她的目標和她找到的東西。
- 不做睡眠狀態機；她凌晨三點也可以工作，只受 energy 和每日上限限制。
- 不推播；除了 event 回合，她不主動開口（agenda、日記、反思和她自己派的 job 都不說話）。
- 不做 omp 式的提醒迴圈、隱藏的「繼續」訊息或要她檢查自己做完沒有的 prompt；主人的事不會單獨叫醒她（§6.4）。
- 不改 energy 公式；不開並行 lane；不保留 slot（R4 量過書桌期間的延遲再說）。
- Mac 本機不跑 GGML 或 torch 的 TTS（Mac 只用 ONNX Runtime）。
- v1 沒有 UI（只有 CLI 和 VMPal 視窗）、沒有 iPhone app、沒有 Android、沒有陌生人。
- v1 不訓練 Piper、不架 TTS server（她講中文時再選一條，§8）、不跑 voice_eval、不做 ASR A/B（除非 dogfood 指出問題）。
- 不讓主人回滾或編輯她的自我；日記分數不進她的 context。
- 執行中不在替代方案之間切換：書桌用哪個後端由 spike 一次決定；TTS 固定是 luxtts，`device` 是 config 值，提示檔是 pack 裡的檔案。
- v1 期間除了這份 spec 和它的 plan，不再開新 spec；點子寫進 parking list。

---

## §14 風險

- **LLM 主機還不知道是哪台**：Qwen3.8 Q4 約 87 GB（網路資料），zenith 放不下。沒有主機，什麼都開始不了。換模型是你的決定，不能偷偷換。
- **電腦操作可能不夠好**：約 6B active 的模型看截圖操作 GUI 可能很差；這個模型有沒有視覺、llama.cpp 能不能在 message 裡帶圖片，都還沒驗證。所以 R1b 就做 spike，不等到核心重建完。
- **VMPal 很新**：改得很快，工具名稱要等 R1b 用 `list_tools` 查。所以要固定版本，允許清單和 sandbox 規則寫在 DollOS 這邊，換成別的 VM 後端也能沿用。
- **2 秒可能做不到**：遠端 MoE、網路、prefill、思考 token。§2 #2 的式子會告訴我們 low 還剩多少思考；如果剩不多，她對話時幾乎不思考，品質可能變差。這條沒過就是 v1 沒過，數字只有你能放寬。
- **書桌時段搶 slot**：背景 worker 佔一個 slot，主人回合可能拿不到快取好的前綴。§2 #2 包括書桌期間的回合，R4 另外驗收。
- **她的電腦生活可能很空**：她可能漫無目的地逛網頁，讀起來像 bot；日記也看不到她讀了什麼（§7 已知的代價）。日記分數會量到這個；怎麼修等資料出來再決定。
- **拿掉 grammar 的品質風險**：舊 smoke 在 step 14（REVIEW 欄位 + 不設上限的 cascade + episodic memory 一起改）從 7/8 升到 8/8；REVIEW 自己的貢獻沒有單獨量過。S8 的盲評是閘門。
- **Mac 的意外**：aiortc、PortAudio、sherpa-onnx 在 macOS 26 的 wheel；麥克風 TCC；16 GB 要放 VM、daemon 和語音，可能吃緊；筆電睡眠會讓她的一天暫停。
- **TTS 和英文**：Gura 的 luxtts 提示只有 3 秒，聽起來可能不像她（不像就用 30 秒的 `ref.wav` 重新編碼，§8）。一整句合成完才出聲，長句的第一個音框會晚。
- **流程**：核可本文、之後七個問題、十三個 spec 修正，對容易超載的使用者仍然多。所以問題一次問一個，修正跟本文一起審，使用迴圈從 R1a 就開始，不是最後才開始。
- **R3 很長**：S5 是最大的單一步驟；刪程式碼只在測試全綠下進行。

---

## §15 要你決定的事（依時間排序，一次問一個）

- **0. 核可本文**（現在）。核可也就同意三件事：(1) §2 是 v1 的終點，從 R1 到 R5 結束不加新的內在生活功能，R4 的書桌是唯一例外（目標與待辦另外問，Q6）；(2) 她自己的時間可以用她的電腦，也就是 agenda 回合可以派 `computer` job，結果降權、不說話、寫進 `desk/`（你 10-17 的方向；這改了 agenda spec 的「自主回合不做外部動作」）；(3) 反思回合不說話、只算私人回合、只在安靜時觸發（mind-loop Q4）。§12 的修正也一起核可。
- **Q1（R0）哪一台機器跑 Qwen3.8-Flash-Next？** 需要 ≥ 128 GB 的 VRAM + RAM，Tailscale/LAN 連得到。
- **Q2（R0，Q1 之後，即 mind-loop Q1）你在 Discord 私訊跟她說話時，算不算「在家」？** 建議 **(a) 算**（principal = owner），因為私訊是你在 v1 唯一的外出管道。mind-loop 當時建議 (b)，理由是 (a) 會讓被盜的 Discord 帳號在你的 Mac 上跑指令；v1 沒有 `run_command`，所以這裡改建議 (a)。(a) 的代價：被盜的帳號可以在她的記憶、目標、排程裡留東西，讓她去 VM 開網頁；私訊的對話也會進日記和反思，幾週下來可能影響她是誰。建議開 Discord 2FA。(b) 是比主人低一階的 `owner_dm`：私訊寫的東西進不了她的私人回合，私訊的對話只以一行紀錄進日記，私訊裡也不能叫她用電腦。
- **Q6（R0，Q2 之後；R1 開始凍結之前）目標與待辦（§6.4）要不要放進 v1？** 建議**放**，在 R3 做（約 150–200 行加測試）：agenda 在 R3 本來就要重寫，之後再加等於寫兩次。代價是凍結多一個例外。不放的話，她還是能記住事情和提醒你（排程那段不管 Q6 都做），清單和步驟等 R5 之後。
- **Q7 她自己寫的東西用什麼語言？** **已決定（2026-10-19，使用者「全改成英文吧」）：她寫的東西全部英文**——筆記、日記、「現在的我」、self-profile、議程、排程（原本建議維持繁體中文）。plan `2026-10-19-doll-all-english.md` 改了：`primary_language` 預設 `"English"`、`[Memory guideline]` 涵蓋她寫下的所有東西、範例筆記英文；daemon 寫進她檔案的標題英文（`## … Diary`、`## … Plan`、self-profile 段落標題），舊的中文標題照樣讀得到；演化的 echo 檢查和斷句支援英文；演化和 consolidation 的 agent prompt 英文，pack 拿掉 `restatement_reference`；字數上限「現在的我」250／2000、self-profile 4000；`ch:` 框架英文。mood 在這之前已改成一個英文字（≤ 12 個字母，預設 `calm`；mind-loop §10，plan `2026-10-19-gura-english-speech-fix.md`）。另一台機器上的舊中文記憶不轉換，只保證讀得到。代價：搜尋是比字的，主人說中文時 `[Memory context]` 的查詢找不到英文筆記，舊的中文筆記也對不上英文查詢；先觀察，要改另寫 plan。
- **Q3（R1b 結束，只在書桌測試不到 8/10 時）書桌怎麼辦？** 三選一：書桌只用 VM 裡的 shell 指令（就是你拒絕當主要介面的那種，只是給她自己用）；改試 Lume + Cua Driver；v1 不做電腦（§2 不變，R4 跳過）。
- **Q4（R3，S4 之後）要不要跑 think A/B 的 B 組（格式化 think 對自由 think + 上限）？**
- **Q5（R3，S8）grammar 路徑還是原生工具？** grammar 路徑沒辦法把截圖當工具結果，選它等於 v1 沒有 GUI 書桌。
