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

- 事件三種：主人語音、主人文字、命令的結果（短命令的結果、背景 job 跑完）。佇列在記憶體；排程點依 `[core] priority` 排進時間線。**有事件才給回合；佇列空的時候程序睡著，不打 Strata。**
- 一回合＝一次模型呼叫。她的回覆裡只有四種工具：一條 ```sh 命令、一行【我想一下】medium|high（下一次呼叫開思考）、【記】秘密|公開（寫一條記憶）、【想】<問題>（查記憶）。其他文字就是對主人說的話。
- 記憶（M3a）：markdown 是原始資料（`<memory.dir>/secret|public`，front-matter 帶來源事件 seq、confidence），SQLite 只是可重建的索引（FTS5 trigram＋embedding）。開機時如果檔數和索引對不上就重建索引。【想】＝FTS＋`NLContextualEmbedding` 餘弦、RRF 融合、confidence 加權，回 top-k 附〔確定／大概／聽說〕。secret 的記憶只進看得到秘密的回合。
- 命令在 `[shell] scratch_dir`（預設 `~/dollos-scratch/`）裡用 `posix_spawn` 跑（`Executor` protocol，M2 換成 VM）。`wait_s` 內跑完 → 結果馬上排成事件（下一回合就看到）；超過 → 轉背景 job，時間線記一句「轉背景」（不給回合），跑完才排成 job 結果事件（exit code＋輸出尾巴 `result_tail_chars`）。命令不擋。
- 時間線在 SQLite `events`（append-only：時間、來源、含不含秘密、內容；主人事件另記語音／文字）。只有主人，所以投影只有一種（全部）。
- 每回合最後一則訊息是【狀況】，只有事實：還在跑的背景 job（id、命令、跑了多久）、context 約幾個 token、下一回合 effort。她回覆後 footer 存進 `footers` 表掛在那則回覆上，之後每回合都照原樣放回 prompt（prefix 不變，Strata 的 checkpoint 才接得上）；不進 `events`。
- 空轉：連續 `[idle] max_turns_without_tool` 輪沒有工具呼叫（沒命令、沒【我想一下】）就寫一行 log，不通知、不限速。
- 主人語音中斷的鉤子 `Doll.interrupt()` 留著，M6 接。

## 驗證

- `swift test`：10 個：回覆解析；佇列依 priority 排；沒事件就沒回合（跑 0.5 秒零次呼叫，進一則主人文字才一次，她的回覆本身不是事件）；短命令結果變事件、長命令變 job 再變 job 事件（含 exit code）；命令在 scratch 裡跑；footer 存表、下一回合照原樣放在她回覆前面、不進 events；【我想一下】只影響下一次呼叫並顯示在【狀況】；連續 N 輪沒工具寫一行 log；重開砍掉上一個程序的 job；設定檔不認得的 key／priority 不全／N<1 報錯。模型是假的，不連 Strata。
- Smoke：`swift run m1-skeleton <tag>` 在真的 dollcore＋Strata 上跑：主人說「幫我在 scratch 建 data/raw 和 data/out，然後跑 `sleep 20 && ls data | wc -l` 看有幾個」→ 她 mkdir（短）→ 跑 sleep（背景 job）→ job 結果事件 → 她回報數字；然後佇列空著 30 秒，證明零次 Strata 呼叫。結果在 `experiments/m1-skeleton/<tag>/events.log`（時間線、每則回覆的 footer、每次呼叫的 `prompt_n`／`cache_n`）。
