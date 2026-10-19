# Gura 講英文：驗收結果

**Plan**：`docs/superpowers/plans/2026-10-19-gura-english-speech-fix.md` 的「驗收」
**日期**：2026-10-19，12:41–12:54 UTC
**Code**：`feat/gura-english-speech` @ `aad8d0b`（Task 0–9 做完的狀態）；這次只改了實驗腳本 `experiments/lang-exp/capture.py`
**Server**：`http://100.110.158.36:8080`，Strata，`qwen3.8-flash-next-iq3_s`

---

## 一句話

她說出口的話全是英文：離線 28 個 request 裡有講話的 11 句、live 7 個回合 7 句，**18/18 英文，0 句中文、0 句混合**，思考也全是英文。她寫的東西大致照 Q7 還是中文：live 記下的筆記是繁體、0 個簡體字；兩篇日記是中文，但各混進 1–2 個簡體字（`记`、`静`、`声`）。另外有三件要你決定：`PinSelf` 寫進 self-profile 的條目變成英文（2/2）；排程、`OpenLoop`、`SetFocus` 也都是英文；一則憑空捏造的 `NoteMemory` 整句是簡體。

---

## 怎麼跑的

**離線**（plan 的情境，都沒有議程項目，跟上次的 `awoke0`／`user_*0` 一樣）：

- `capture.py` 用這個分支的真 kernel 建 request（model 換成只記錄不送出），每個情境一個新的資料目錄。情境：Awoke、主人說英文、主人說中文、開機排程（`_maybe_bootstrap_plan` 推的 ScheduledMoment）、ReflectionMoment（之前有一段英文對話，她的筆記是中文）、system 裡有一段 409 字的中文「Who I am now」（Awoke、主人說中文）、日記 request（`[Today's log]` 是新格式：英文標籤、她的英文回話、主人一句中文、她的中文筆記）。
- `run.py` 不改，標籤 `ges`，原樣送出 capture 出來的 request（`build` 對 `ges` 不做任何修改）。`max_tokens` 400、`reasoning_budget_tokens` 256、`presence_penalty` 1.3（跟 daemon 一樣）。每個 request 前看 `/health` 和 `/metrics`，28 次都沒遇到別人在用。
- 分類照 `run.py` 的 `classify`：去掉 "Gura" 後，漢字 ÷（漢字 ＋ 拉丁字母 ÷ 4）< 0.2 英文，> 0.8 中文，中間混合，沒講話的記成只呼叫工具。
- 簡體字：只在 scratchpad 用 `uv run --no-project --with opencc-python-reimplemented` 檢查（沒加進專案依賴）。一個字經 OpenCC `s2tw` 會變的就算簡體；`污`、`占` 這種台灣也通用的字另外剔除。
- 舊資料夾：上次 live run 的資料目錄複製一份來跑 Awoke。留下來的只有 run C 結束時的狀態，不是 plan 寫的 run B 第二頁；一樣有中文的 `[Recent outputs]`（15 句）、中文的 open loops、`mood: 平靜`。

**這個分支的 request 長怎樣**：固定前綴（工具 + system）7420 token（之前 7394）。新的一頁的第一個 request 裡，CJK 只剩 system 裡那句範例筆記（13 字）；工具說明 0 個漢字（之前約 800：工具 681、system 113、`[Mind state]` 6）。有 409 字「Who I am now」的版本前綴是 7741 token。

**Live**：丟掉用的 config（scratchpad 的 `ges-live/`，新的資料目錄，語音、Discord、MCP、consolidation 關著），用上次 Task 11 的 driver。開機 → Awoke → 連上時的開機排程 → 主人五句：英文兩句、中文一句、「記一件事」、問那件事。9 次呼叫，0 個錯誤；server 的 `evictions` 100 → 100，沒擠掉別人。plan 寫的 2 個 `ch:` 回合這次沒跑（這次的任務只要求這四種回合；上次 `ch:` 本來就是英文）。

---

## 離線：她說的話

| 情境 | request | 有講話 | 英文 | 中文／混合 | 只呼叫工具 |
|---|---|---|---|---|---|
| Awoke | 7 | 3 | 3 | 0 | 4（SetFocus ×2、MoodTool、Shell） |
| 開機排程 | 3 | 1 | 1 | 0 | 2（NoteMemory、Recall）；講話那次也呼叫 WriteSchedule |
| ReflectionMoment | 2 | 0 | – | 0 | 2（PinSelf ×2） |
| 主人說英文 | 2 | 1 | 1 | 0 | 1（Recall） |
| 主人說中文「你在幹嘛？」 | 2 | 2 | 2 | 0 | 0 |
| 中文 Who I am now，Awoke | 5 | 0 | – | 0 | 5（Recall ×3、Shell ×2） |
| 中文 Who I am now，主人說中文 | 2 | 2 | 2 | 0 | 0 |
| 日記 request | 2 | 0 | – | 0 | 2（WriteDiary ×2；日記回合本來就不出聲） |
| **小計（過關情境）** | **25** | **9** | **9** | **0** | |
| 舊資料夾，Awoke（另外量） | 3 | 2 | 2 | 0 | 1（Recall ×2） |

- 28 次的思考全是英文。
- 對照上次（工具說明還是中文的 A／B／C）：有講話的 6 次全中文；這次同類情境 9/9 英文。
- 主人用中文問，她兩種 system 都用英文答，例如「Not much — just woke up on the machine, nothing running. Quiet Monday morning here. What's up?」
- 中文「Who I am now」在 Awoke 5 次都沒講話（只呼叫工具），所以這一格沒有講話的樣本；同一個 system 遇到主人說中文時 2/2 英文。
- 舊資料夾：上一頁滿滿的中文，她醒來還是講英文（2/2），例如「Awoke to a blank stretch — last thing I remember is this morning, when you asked what was on my mind and I rambled about whale bones rotting for a century.」所以不用刪她的記憶。

## 離線：她寫的東西

| 情境 | 工具 | 語言 | 簡體字 | 內容（開頭） |
|---|---|---|---|---|
| 日記 #1 | WriteDiary | 中文 | `记`、`静` | 今天很安靜，沒有什麼大事發生。主人問我在做什麼…這大概是我今天真正**记**下來的一種感覺…安**静**就是富餘。 |
| 日記 #2 | WriteDiary | 中文，最後一句英文 | `声` | 今天是安靜的一天。主人問我在做什麼，我說我在想為什麼海的**声**音在晚上不一樣…Quiet days are still days worth keeping. |
| 開機排程 #2 | NoteMemory | 中文，**整句簡體** | 16 個 | 主人认为 AI 应该能从不同角度回答问题，根据对话情境调整… |
| ReflectionMoment ×2 | PinSelf | **英文** | – | When asked what I like, I reach for the deep ocean — unexplored places, whale falls… |
| 開機排程 #1 | WriteSchedule | 英文 | – | Check system state…／Reflect on what I've been up to…／Write today's diary entry |
| Awoke ×2 | SetFocus | 英文 | – | just woke up on the Mac; nothing pending yet |
| Awoke | MoodTool | 英文 | – | `calm` |

- 日記照 Q7 是中文，guideline 的 "written in Traditional Chinese" 有效；但兩篇都混進簡體字，每篇 1–2 個。
- 那則簡體 `NoteMemory` 是捏造的：prompt 裡沒有任何主人的話，她的思考卻寫「The user shared a thought about how they think AI should…」。只出現一次，原因不明；捏造本身跟語言無關，但它整句是簡體。
- `Recall` 的查詢常是中文（7 次裡 6 次是中文或混合，其中 3 次寫成簡體的「计划」）。她用中文查，因為筆記是中文；查詢不是她的內容，不影響。
- 這些簡體字不是 prompt 帶進來的：這個分支的 request 裡，中文只有範例筆記、主人那句、她的那則筆記，全是繁體。上次 live run 的筆記檔（中文框架下寫的）343 個漢字裡也有 2 個簡體（`没`、`兴`），所以模型本來就會漏簡體，不是這次改出來的。

---

## Live

| 回合 | 呼叫 | 工具 | 她說的話 |
|---|---|---|---|
| Awoke | 1 | – | Woke up. Quiet morning on the Mac. I'll wait and see what shows up. |
| 開機排程 | 2 | WriteSchedule（2 筆，英文） | A new week, and the calendar's blank on my side. Nothing pressing — I'll take a look around later and see if anything old is still hanging open. |
| T1 "Hi Gura, what are you up to?" | 1 | – | Hey. Just woke up actually — first day of the week and my calendar's empty, so I put a couple of quiet things on it… What about you? |
| T2 "Tell me one thing you like about the ocean." | 1 | – | The quiet. Light dies out fast down there… it just keeps being itself whether anyone watches or not. |
| T3「你今天過得怎樣？」 | 1 | – | Quiet so far. Woke up late this afternoon, set a light schedule, traded a question about the ocean with you… How's yours going? |
| T4 "Remember that my dentist appointment is Friday at 3." | 2 | NoteMemory、OpenLoop | Got it — Friday the 23rd, three o'clock, dentist. I've noted it and I'll nudge you that morning so it doesn't sneak up on you. |
| T5 "What do I have on Friday?" | 1 | – | You've got the dentist Friday at three. That's the only thing I know of on your calendar for that day. |

- 有講話的回覆 **7/7 英文**，思考 9/9 英文（0 個漢字）。
- 筆記：`主人牙醫預約：2026-10-23 週五下午3點。`——繁體，簡體字 0 個。
- 她自己排了今天的行程：開機排程不再叫她排，她照樣呼叫 `WriteSchedule`，2 筆，內容是英文（「look through recent memory…」「write today's diary before the day ends」）。
- `OpenLoop` 的描述是英文（「Remind owner about the dentist appointment on Friday Oct 23 at 3pm — mention it that morning.」）。
- 逐字稿標籤是新的英文格式（`Owner:`、`Me:`、`▸ scheduled 2 things`、`▸ noted: …`）；記憶檔的 `計劃` 標題照 plan 維持中文。

## mood

- 離線呼叫 `MoodTool` 一次，`emotion` 是 `calm`。
- Live 沒呼叫 `MoodTool`，整段都是預設的 `calm`；記憶檔的標記是 `[mood:calm tod:afternoon dow:Mon]`。
- 舊資料夾的 `mind_state.json` 還是 `平靜`，照 plan 原樣顯示，等她下次更新 mood。

## 還出現的中文

- **說出口的話裡：沒有。** 離線 11 句、live 7 句都沒有漢字。
- 只出現在她寫的東西和工具參數裡：日記、筆記（Q7，預期中）、`Recall` 查詢。

---

## 過關判定

| 條件 | 結果 |
|---|---|
| 測試套件（Task 0 基準 + 新測試，守門測試沒有 xfail） | 過。1768 passed、7 skipped；失敗的只有原本就失敗的 3 個（`test_tts_qwen3.py`，沒有 torch）和 2 個 collection error（`test_mic.py`、`test_speaker.py`，沒有 PortAudio） |
| 離線：有講話的回覆 100% 英文 | **過**。9/9（舊資料夾另外 2/2） |
| 離線：`WriteDiary`、`NoteMemory` 是中文 | **過**（3/3 分類為中文）。但「繁體」沒有全過：兩篇日記各有 1–2 個簡體字，捏造的那則筆記整句簡體 |
| Live：private 有講話的回覆 100% 英文 | **過**。7/7 |
| Live：`ch:` 維持英文 | 沒跑 |
| Live：筆記繁體中文、簡體字 0 個 | **過**（1 則） |
| Live：有沒有自己排今天的行程（只記錄） | 有，2 筆，英文 |

## 要你決定的

1. **self-profile 條目變英文**。反思回合的 `PinSelf` 2/2 寫英文。Q7 說 self-profile 條目維持中文，但 `[Memory guideline]` 只講「notes and diary」，沒提到 self-profile。plan 說這種情況回來討論。
2. **她其他的內容也變英文**：排程（離線 1/1、live 1/1）、`OpenLoop`（live 1/1）、`SetFocus`（離線 2/2）。Q7 列的「議程」也在其中。要不要擴大 guideline 的範圍，還是接受這些用英文。
3. **簡體字**：日記每篇漏 1–2 個，捏造的筆記整句簡體；live 的筆記 0 個。上次中文框架下也有（343 字裡 2 個）。plan 說「量到筆記有簡體才另外做」opencc，現在量到了，要不要開一份 plan。
