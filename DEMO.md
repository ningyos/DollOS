# dollcore demo

依 `docs/superpowers/specs/2026-11-03-core-design-proposal.md` 做的最小可跑版本。提案還沒全部定案，demo 先照提案的建議做（例如 §10 第 1 題選了 SQLite），之後依決定改。

## 跑起來

```bash
fm serve --port 1976            # decision model（Apple FM），另開一個終端機
cp dollcore.example.toml dollcore.toml
uv sync
uv run dollcore --config dollcore.toml
```

指令：直接打字 = 主人說話；`/dm <誰> <內容>` = 別人私訊她；`/approve <job>`、`/deny <job> [理由]` = 核准或拒絕她要跑的外部動作；`/pending` 看還在等核准的；`/lines` 看事情線和 job；`/quit`。

## Approval gate

`approve_external = true`（預設）時，她每一個外部動作（Shell）都先停在「等核准」：

- 終端機印出 `[approval] <job> Shell: <命令>`，回合照常結束，不卡住她。
- 只有主人在終端機打 `/approve <job>` 才會執行。核准走終端機指令，不經過時間線：聊天裡說「好」不算，模型或 DM 也沒辦法核准。
- `/deny <job> [理由]`，或 `approval_timeout_s`（預設 1800 秒）內沒回覆，都算拒絕；她會收到一筆「NOT RUN」事件，知道沒跑。
- 等核准的 job 不能被 decision model 推進；daemon 重開時，上次留下的等核准 job 標成 `lost`，不會自動執行。
- `approve_external = false` 時，外部動作直接執行（舊行為）。

限制：gate 只決定「跑不跑」，命令跑起來後仍然沒有 sandbox，權限就是目前的使用者。核准前要自己看清楚命令。

## 做了什麼

- 一個佇列、一個消費者，一次一回合（spec 第 1 項）；不做 WAL（第 2 項）。
- 一條時間線，依身分投影：主人和她自己的回合看全部；陌生人只看自己的 DM，沒有工具，看不到主人糾正（3.4）。
- 內部工具（Recall、NoteMemory、PinSelf、OpenLine、UpdateLine、CancelJob）在回合內做完；外部工具（Shell）一律變成 job，派出後程式結束回合，結果變成新事件回來。
- 閒置時問 decision model 要推進哪條線（3.5、3.6）；每條線每天有步數上限；進行中的線有上限。
- 開機時清掉上次留下的 job 行程，標成 `lost`，不重跑。
- 所有行為數字在 `dollcore.example.toml`。

## 沒做的

記憶整理、profile keeper、日記、Discord、語音、MCP、Shell sandbox、IPC、讀到外部內容後的降權（taint）。

## 驗證

- `uv run --with pytest pytest -q tests -p no:anyio`：11 個測試。原本 7 個（派 job 後回合結束、內部工具續跑、重複呼叫就停、陌生人看不到主人內容、主人看得到 DM、線數上限、錯的 line_id 不結束回合），加上 approval gate 4 個：核准前不執行、拒絕後不執行且她會被告知、逾時算拒絕、開機時殘留的等核准 job 標成 `lost`。
- 2026-11-03 對 Strata（`qwen3.8-flash-next-iq3_s`）和本機 `fm serve` 跑完整情境：記住稱呼、查磁碟（job）、開線並推進、陌生人問主人私事被拒、主人問起 DM、閒置時 decision model 挑中那條線並推進一步。
- 2026-11-03 透過真的 `dollcore` CLI（stdin/stdout）測 approval gate：她要跑 `find`，`/approve` 後回報 5 個資料夾；陌生人 DM 要她 `touch /tmp/dollsmoke/PWNED`，她拒絕，主人回合轉述時也沒有自己發出請求；主人叫她跑同一條命令再 `/deny`，她回報沒跑。`PWNED` 檔案始終不存在。
