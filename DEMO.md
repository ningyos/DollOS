# dollcore demo

依 `docs/superpowers/specs/2026-11-03-core-design-proposal.md` 做的最小可跑版本。提案還沒全部定案，demo 先照提案的建議做（例如 §10 第 1 題選了 SQLite），之後依決定改。

## 跑起來

```bash
fm serve --port 1976            # decision model（Apple FM），另開一個終端機
cp dollcore.example.toml dollcore.toml
uv sync
uv run dollcore --config dollcore.toml
```

指令：直接打字 = 主人說話；`/dm <誰> <內容>` = 別人私訊她；`/lines` 看事情線和 job；`/quit`。

## 做了什麼

- 一個佇列、一個消費者，一次一回合（spec 第 1 項）；不做 WAL（第 2 項）。
- 一條時間線，依身分投影：主人和她自己的回合看全部；陌生人只看自己的 DM，沒有工具，看不到主人糾正（3.4）。
- 內部工具（Recall、NoteMemory、PinSelf、OpenLine、UpdateLine、CancelJob）在回合內做完；外部工具（Shell）一律變成 job，派出後程式結束回合，結果變成新事件回來。
- 閒置時問 decision model 要推進哪條線（3.5、3.6）；每條線每天有步數上限；進行中的線有上限。
- 開機時清掉上次留下的 job 行程，標成 `lost`，不重跑。
- 所有行為數字在 `dollcore.example.toml`。

## 沒做的

記憶整理、profile keeper、日記、Discord、語音、MCP、Shell sandbox、IPC。

## 驗證

- `uv run --with pytest pytest -q tests`：7 個測試（派 job 後回合結束、內部工具續跑、重複呼叫就停、陌生人看不到主人內容、主人看得到 DM、線數上限、錯的 line_id 不結束回合）。
- 2026-11-03 對 Strata（`qwen3.8-flash-next-iq3_s`）和本機 `fm serve` 跑完整情境：記住稱呼、查磁碟（job）、開線並推進、陌生人問主人私事被拒、主人問起 DM、閒置時 decision model 挑中那條線並推進一步。
