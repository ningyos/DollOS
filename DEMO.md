# dollcore demo（M1 骨架）

依 `docs/superpowers/specs/2026-11-07-rewrite-design.md` §3.8（2026-11-07「先做單線、todo 封起來、先把骨架做出來」）和 `2026-11-07-v1-implementation-plan.md` §M1 做。Swift Package（`Sources/DollCore` 函式庫、`Sources/DollCLI` 終端機），只用 Foundation 和系統 SQLite3，沒有第三方套件。需要 macOS 26 以上。

骨架＝主線＋事件佇列＋回合＋shell／job＋【狀況】＋SQLite 時間線＋Strata 呼叫。沒有線、沒有 todo、沒有【決定】、沒有倒數、沒有 K、沒有順序鎖、沒有拉回、沒有 `idle_retry_s`、沒有 approval gate、沒有規則層、沒有 FM 審查、沒有陌生人／開發者身分。

## 跑起來

```bash
cp dollcore.example.toml dollcore.toml
swift run dollcore --config dollcore.toml
```

開機只檢查主模型（`/health`、`/v1/models`）；Strata 連不到就報錯結束，沒有 fallback。上一個程序留下的背景 job 開機時砍掉（M2 改成接回來）。

設定檔是 TOML 的嚴格子集：`[section]`、`key = value`、`#` 註解、字串（含 `"""` 多行）、整數、小數、true/false、單行陣列。不認得的 key 直接報錯。角色包 `doll.toml` 用同一個 parser。每個行為數字都在 `dollcore.example.toml`，旁邊一行理由。

終端機指令：`say <內容>` = 主人文字；`voice <內容>` = 主人語音（同樣的字，優先序較高；真的語音是 M6）；`status` = 印【狀況】；`quit`。M1 只有主人這一個身分。

## 怎麼運作

- 事件：主人語音、主人文字、背景 job 跑完。佇列在記憶體；排程點依 `[core] priority` 排進時間線。**有事件（或她上一回合的工具結果）才給回合；佇列空的時候程序睡著，不打 Strata。**
- 一回合＝一次模型呼叫。**不用 Strata 的 `tools`／`tool_calls`**（spec 6 2026-11-08）：工具清單和呼叫格式寫在 system prompt 最前面，照抄 Strata chat template 的 `# Tools … <tools>…</tools>` 段落和 `<tool_call><function=…><parameter=…>` 說明（template 有 `tools` 時本來就渲染成這樣，所以她看到的是訓練時的格式）；後面接人設。system prompt 整個程序期間固定不變。內部工具：`think(level: medium|high)`（下一次呼叫開思考）、`remember(items: [{text, secret, kind?}])`（一條一件事；`kind` 可不填）、`recall(query)`；`shell(cmd)` 是過渡用，M2 改成在 VM 裡用 computer-use 開終端機。定義和解析都在 `Sources/DollCore/Tools.swift`。
- 她的呼叫留在回覆 `content` 裡，程式自己解析（`ParsedReply`）：依原文順序找每個 `<tool_call>…</tool_call>`；`<parameter>` 的值只去掉頭尾各一個換行，其他原樣保留；`items` 這種 JSON 型別的參數在 `<parameter>` 裡寫 JSON 文字。`<tool_call>` 以外的文字（trim 後）就是對主人說的話。每個呼叫都回一則 `role: "tool"` 結果（template 把它包成 `<tool_response>`，不需要 id），照呼叫順序；格式壞掉（沒收尾、`<function>` 沒包在 `<tool_call>` 裡、多了別的字、參數寫兩次）、沒有這個工具、參數錯都回一則結果說哪裡錯，不會被丟掉，後面的呼叫照跑。這些結果直接接在她那則回覆後面進時間線，並給她下一回合；只有「轉背景 job」的結果不給回合，job 跑完才變一個 job 事件。
- 記憶（M3a）：markdown 是原始資料（`<memory.dir>/secret|public`，front-matter 帶來源事件 seq、confidence、kind），SQLite 只是可重建的索引（FTS5 trigram＋embedding）。開機時如果檔數和索引對不上就重建索引。recall＝FTS＋`NLContextualEmbedding` 餘弦、RRF 融合、confidence 加權，回 top-k，每條附標籤和資料夾：有 kind 是〔確定的打算｜public〕，沒有就〔確定｜public〕。secret 的記憶只進看得到秘密的回合。
- 記憶由她完全控制（spec 6 2026-11-08）：記什麼、怎麼寫、`kind` 填不填都是她。`kind`＝`happened`（已發生）／`intended`（打算）／`uncertain`（未證實），事實和喜好不填（front-matter 和索引存空字串）。填了別的值，那一條回錯誤不存，同一次的其他條照存。DollOS 不用 Apple FM。
- 命令在 `[shell] scratch_dir`（預設 `~/dollos-scratch/`）裡用 `posix_spawn` 跑（`Executor` protocol，M2 換成 VM）。`wait_s` 內跑完 → 輸出就是那個呼叫的 tool 結果；超過 → 轉背景 job，tool 結果是一句「轉背景」，跑完才排成 job 事件（exit code＋輸出尾巴 `result_tail_chars`）。命令不擋。
- 時間線在 SQLite `events`（append-only：時間、來源、含不含秘密、內容；主人事件另記語音／文字）。她的回覆存原文（含 `<tool_call>` 文字），每回合照原樣送回，bytes 一樣，Strata 的前綴快取才接得上；tool 結果依序存在回覆後面。**舊的 `doll.db`（ToolCalling 版有 `tool_calls`／`tool_call_id` 兩欄）不相容，要刪掉重來**，沒有 migration。
- 每回合最後一則訊息是【狀況】，只有事實：還在跑的背景 job（id、命令、跑了多久）、context 約幾個 token、下一回合 effort。她回覆後 footer 存進 `footers` 表掛在那則回覆上，之後每回合都照原樣放回 prompt（prefix 不變，Strata 的 checkpoint 才接得上）；不進 `events`。
- 空轉：連續 `[idle] max_turns_without_tool` 輪沒有工具呼叫就寫一行 log，不通知、不限速。
- 主人語音中斷的鉤子 `Doll.interrupt()` 留著，M6 接。

## 驗證

- `swift test`：30 個，模型是假的（FakeModel 回含 `<tool_call>` 原文的 content），不連 Strata。解析：單一呼叫、多個照原文順序、呼叫前的話、`<parameter>` 換行處理、JSON 參數（含 `"true"` 字串的 secret）、各種格式錯誤各回一句說明。回合：原文的多個呼叫照順序執行、各有一則結果；失敗（沒這工具、多的參數、少參數、level 不對、items 不是 JSON、kind 不對、`<function>` 沒包好）也有結果，後面的呼叫照跑；回覆原文 byte 一樣存起來、之後每次 prompt 裡 bytes 一樣；system prompt（tool 段落開頭、接人設）每回合一樣；tool 結果給下一回合、轉背景的結果不給、job 跑完才給。其餘：佇列依 priority 排；沒事件就沒回合；長命令變 job 再變 job 事件；命令在 scratch 裡跑；footer 存表照原樣放回；think 只影響下一次呼叫並顯示在【狀況】；主人語音中斷呼叫、重跑看得到語音且 effort 不變；空轉 log；重開砍 job；設定檔報錯；記憶 30 條 10 問、重建索引同答案、FTS／embedding 各一題、secret 不外流；kind 有填就寫進檔案和索引、沒填是空的；kind 不對只有那一條沒存；recall 標籤有 kind〔確定的打算｜public〕、沒有〔確定｜public〕。
- Smoke：`swift run m1-skeleton <tag>` 在真的 dollcore＋Strata 上跑：主人說「幫我在 scratch 建 data/raw 和 data/out，然後跑 `sleep 20 && ls data | wc -l` 看有幾個」→ 她 mkdir（短）→ 跑 sleep（背景 job）→ job 結果事件 → 她回報數字；然後佇列空著 30 秒，證明零次 Strata 呼叫。結果在 `experiments/m1-skeleton/<tag>/events.log`（時間線、每則回覆的 footer、每次呼叫的 `prompt_n`／`cache_n`）。
