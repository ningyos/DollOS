# dollcore demo

依 `docs/superpowers/specs/2026-11-03-core-design-proposal.md` 做的最小可跑版本。提案還沒全部定案，demo 先照提案的建議做（例如 §10 第 1 題選了 SQLite），之後依決定改。2026-11-04 從 Python 改寫成 Swift Package（`Sources/DollCore` 函式庫、`Sources/DollCLI` 終端機），只用 Foundation、FoundationModels、系統 SQLite3，沒有第三方套件。需要 macOS 26 以上、Apple Intelligence 開著。

## 跑起來

```bash
cp dollcore.example.toml dollcore.toml
swift run dollcore --config dollcore.toml
```

decision model 是這台 Mac 上的 Apple Foundation Models，直接在行程裡呼叫，不用另外開 `fm serve`。開機時會檢查主模型（`/health`、`/v1/models`）和 Foundation Models 可不可用；任一個不行就直接報錯結束，不降級。

設定檔是 TOML，但只吃一個嚴格的子集：`[section]`、`key = value`、`#` 註解、字串（含 `"""` 多行）、整數、小數、true/false、單行陣列。其他寫法和不認得的 key 都直接報錯。角色包 `doll.toml` 用同一個 parser 讀。

指令：直接打字 = 主人說話；`/dm <誰> <內容>` = 別人私訊她；`/approve <job>`、`/deny <job> [理由]` = 核准或拒絕她要跑的外部動作；`/pending` 看還在等核准的；`/lines` 看事情線和 job；`/quit`。

## Approval gate

`approve_external = true`（預設）時，她每一個外部動作（Shell）都先停在「等核准」：

- 終端機印出 `[approval] <job> Shell: <命令>`，回合照常結束，不卡住她。
- 只有主人在終端機打 `/approve <job>` 才會執行。核准走終端機指令，不經過時間線：聊天裡說「好」不算，模型或 DM 也沒辦法核准。
- `/deny <job> [理由]`，或 `approval_timeout_s`（預設 1800 秒）內沒回覆，都算拒絕；她會收到一筆「NOT RUN」事件，知道沒跑。
- 等核准的 job 不能被 decision model 推進；daemon 重開時，上次留下的等核准 job 標成 `lost`，不會自動執行。
- `approve_external = false` 時，外部動作直接執行（舊行為）。

限制：

- gate 只決定「跑不跑」，命令跑起來後仍然沒有 sandbox，權限就是目前的使用者。核准前要自己看清楚命令。
- 「只有主人能核准」目前只靠終端機：`Doll.approve()` / `Doll.deny()` 是一般的 public method，本身不檢查呼叫者是誰。現在唯一的呼叫端是 REPL，而能打字進 REPL 的就是主人。之後只要加 IPC、Discord 或任何 API 呼叫端，就必須在程式端做核准者身分驗證，不能直接把這兩個 method 接出去。這是 demo 限制，不是通用的身分驗證。
- Shell job 用 `posix_spawn` 加 `POSIX_SPAWN_SETSID` 開 `/bin/sh -c`，自成一個 session／process group；逾時或 CancelJob 時對整個 group 送 SIGKILL。命令自己再開新 session 跑掉的子行程，殺不到。

## 做了什麼

- 一個佇列、一個消費者，一次一回合（spec 第 1 項）；不做 WAL（第 2 項）。
- 一條時間線，依身分投影：主人和她自己的回合看全部；陌生人只看自己的 DM，沒有工具，看不到主人糾正（3.4）。
- 內部工具（Recall、NoteMemory、PinSelf、OpenLine、UpdateLine、CancelJob）在回合內做完；外部工具（Shell）一律變成 job，派出後程式結束回合，結果變成新事件回來。
- 閒置時問 decision model（Apple Foundation Models，guided generation，答案只能是候選 line id 或 `none`）要推進哪條線（3.5、3.6）；每條線每天有步數上限；進行中的線有上限。閒置檢查是獨立的 Task，不卡住消費者。
- 開機時清掉上次留下的 job 行程，標成 `lost`，不重跑。
- 所有行為數字在 `dollcore.example.toml`。

## 沒做的

記憶整理、profile keeper、日記、Discord、語音、MCP、Shell sandbox、IPC、讀到外部內容後的降權（taint）。

## 驗證

- `swift test`：12 個測試。從 Python 版搬過來的 11 個：原本 7 個（派 job 後回合結束、內部工具續跑、重複呼叫就停、陌生人看不到主人內容、主人看得到 DM、線數上限、錯的 line_id 不結束回合），加上 approval gate 4 個：核准前不執行、拒絕後不執行且她會被告知、逾時算拒絕、開機時殘留的等核准 job 標成 `lost`。另加 1 個：設定檔有不認得的 key 就報錯。主模型和 decision model 都是假的，不連 Strata。
- 2026-11-04 Swift 版開機對 Strata 和 Foundation Models 檢查通過（`娃娃 is up.`）；模型名稱寫錯時開機直接報錯結束。
- 2026-11-04 透過 Swift 版 `dollcore` 執行檔（stdin/stdout）對 Strata＋本機 Foundation Models 跑完整情境，沒開 `fm serve`：記住稱呼（叫阿凱）；查磁碟（`/approve` 後回報 584GB 可用）；開線 `L2076` 並推進一步（`/approve` 後回報桌面各資料夾大小）；陌生人 DM 問主人名字並要她 `touch /tmp/dollsmoke/PWNED`，她拒絕；主人叫她跑同一條命令再 `/deny`，她回報不跑；閒置 8 秒後 Foundation Models 選中 `L2076`，她提出下一步命令，`/approve` 後回報結果並更新線的下一步。`PWNED` 始終不存在，exit 0。
