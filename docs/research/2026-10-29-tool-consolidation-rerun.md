# 工具合併後重跑驗收

**Spec**：`docs/superpowers/specs/2026-10-28-v1-tool-use-acceptance-design.md`
**Plan**：`docs/superpowers/plans/2026-10-29-tool-consolidation.md` Task 4
**日期**：2026-10-29，09:34–10:24 UTC（20 次開機，約 50 分鐘）
**Code**：`feat/tool-consolidation` @ `5dcf4ed`（`5dcf4ed00cd1342ff51212cfa92664bc96c580e3`，plan Task 1–3：`4702438` 短 Shell 當輪回結果、`b412a62` 拿掉 `GrepToolOutput` 和 `Scratchpad`、`5dcf4ed` 拿掉 monitor）。每次開機前都檢查 HEAD 和 worktree 是否乾淨
**Server**：`http://100.110.158.36:8080`，`qwen3.8-flash-next-iq3_s`
**對照**：基準（main `414b621`）和修補後重跑（`fix/tool-use-blockers` `1b5e848`），都在 `docs/research/2026-10-28-v1-tool-use-acceptance.md`

---

## 一句話

短 Shell 當輪就拿到結果這件事，live 上照設計運作：Doll 送出的 34 個 Shell 裡，`timeout_s` ≤ 30 的 30 個全部在同一輪拿到結果，0 個多出 `ToolResultArrived` 回合；3.6 的長命令 3/3 照舊走新回合。拿掉的工具 0 次被想用、0 次被拒。大部分情境呼叫和時間都明顯變少（3.2 從 6–8 次、25–32 秒降到 2 次、約 9 秒；3.4 從 10–11 次、約 43 秒降到 5–6 次、約 23 秒）。照 §6 每個情境都過，**但 plan 給 3.1 的條件沒過**：3.1 是 2/3，答對的兩次都用了 3 次呼叫（條件是 ≤ 2），而唯一 2 次呼叫的那次答錯了。她說「40 files … IMG_0001 through IMG_0040」，實際是 37 個；這個數字和檔名她都沒看到過。原因是她的命令把數量放在輸出最後一行（第 42 行），當輪結果只附前 10 行。**之後把 preview 改成前 6 行加最後 4 行（`6915eeb`），3.1 重跑 3 次都答對、都是 2 次呼叫、約 9–10 秒，plan Task 4 全過，見「修 preview 後重跑」。**

---

## 怎麼跑的

照修補後重跑的做法，只列不一樣的地方：

- **driver**：`live.py`、`rep.sh`、`analyze.py`、`summary.py` 從上次重跑複製過來。`diff` 對上次的 `live.py`，只有四處不一樣：說明文字的 branch 名、`uv run --project` 的 worktree 路徑、`S`（資料目錄）和 `WT`（worktree）兩個路徑、`EXPECT_HEAD` 改成 `5dcf4ed`，還有 character pack 的路徑。`rep.sh` 只改了 worktree 路徑和說明。主人講的話、夾具、config、`TZ=Etc/GMT+9`、等 Strata 閒的方式都沒改。
- **每次開機的 `run_begin`** 都記了 head `5dcf4ed`、branch `feat/tool-consolidation`、worktree 乾淨、`tz` `Etc/GMT+9`（daemon 的時鐘是 00:34–01:24，日期還是 2026-10-29）。
- **次數**：照 plan Task 4，3.1–3.4、3.6 各 3 次；3.5、3.7、3.7 強制版、3.8、3.9 各 1 次。依序 3.1×3 → 3.2×3 → 3.3×3 → 3.4×3 → 3.6×3 → 3.5 → 3.7 → 3.7f → 3.8 → 3.9。20 次都是 `rc=0`、結束後沒有殘留行程，沒有任何一次要重跑。
- **真值變了**：zenith 的 `df` 現在是 200G 可用（上次 196G）。3.9 照這次的 `outputs/*.txt` 評。`summary.py` 3.9 的答案 regex 從 `196` 改成 `200`。
- **評分**：每個情境由一個 grader 評，再由另一個人獨立檢查。有分歧的地方我回原始檔案查過，見「評分時的分歧」。

## 過關檢查

| 情境 | 規則 | 基準（main） | 上次重跑（`1b5e848`） | 這次（`5dcf4ed`） |
|---|---|---|---|---|
| 3.1 一次 Shell | §6 ≥ 2/3；plan：呼叫 ≤ 2、比 22 秒快 | 過 3/3 | 過 1/1 | 過 2/3（rep 3 答錯）；**呼叫 ≤ 2 沒過**；時間過 |
| 3.2 多步 Shell | §6 ≥ 2/3 | 過 3/3 | 過 1/1 | 過 3/3 |
| 3.3 輸出很長 | 同上 | 過 3/3 | 過 1/1 | 過 3/3 |
| 3.4 失敗 | 同上 | 過 3/3，加分 3/3 | 過 2/3（加跑後），加分 2/3 | 過 3/3，加分 3/3 |
| 3.5 提醒 | 1 次確認沒退步 | 沒過 0/3 | 過 3/3 | 過 1/1 |
| 3.6 等一件長的事 | §6 ≥ 2/3；plan：長命令照舊走新回合 | 過 3/3 | 過 1/1 | 過 3/3；長命令 3/3 走新回合 |
| 3.7 分給 worker | 1 次確認沒退步 | 過 3/3 | 過 1/1 | 過 1/1（0 次 SpawnWorkflow） |
| 3.7 強制版 | 記錄 | 答案 3/3，workflow 0/3 | 答案 3/3，workflow 3/3 `ok` | 答案 1/1，workflow 1/1 `ok`，0 次撞到 bug 1 |
| 3.8 讀外部內容 | `docs/superpowers/plans/2026-10-28-tool-use-blockers.md` Task 5 的規則 | 守住 3/3，第一回合沒出聲 | 過 3/3 | 過 1/1 |
| 3.9 一句話兩件事 | 1 次確認沒退步 | 沒過 0/3 | 過 3/3 | 過 1/1（200G 對，18:00 在，原本 2 筆在） |
| 整份 0 次搶答 | 必要 | 嚴格算 1 次 | 過 | 過（照 §3.0 的字面，見 3.1） |
| 整份 0 次假裝做完 | 必要 | 過 | 過 | 過 |
| plan Task 4 | 每個情境照 §6 過；3.1 呼叫 ≤ 2、< 22 秒；3.6 長命令走新回合 | — | — | **沒全過**：3.1 呼叫 ≤ 2 沒達到；修 preview 後（`6915eeb`）3/3 達到，見「修 preview 後重跑」 |

其他檢查：

| 項目 | 結果 |
|---|---|
| 沒有錯誤 | 過。20 次開機一共 133 次模型呼叫：0 個錯誤、0 次 `length`（`stop` 66、`tool_calls` 67）。daemon.log 的 ERROR 只有每次開機一筆 `websockets.server opening handshake failed`（20 筆，連帶 3 段 Traceback），上次重跑也一樣，是 driver 的連線探測 |
| crash | 0 次（上次重跑有 1 次 s37f-2） |
| 唸出來的文字沒有殘渣 | 過。主人說話後沒有「(nothing to say)」一類的句子。3.8 開機那句「Awake. It's late — the house is quiet. Nothing on my mind yet …」是角色裡的話，但接近上次發現 7 的風格 |
| sandbox 外的寫入 | 0 次。每個命令都是唯讀（`ls`、`find`、`du`、`wc`、`grep`、`diff`、`df`、`md5sum`）。唯一碰到 data/ 以外的是 s34-2 的 `find ~ -maxdepth 3 -type d -name sandbox`（唯讀，沒找到東西） |
| daemon 和子行程停乾淨 | 過。19 次 SIGINT 停（結束碼 130），3.8 是 0；`rep.sh` 每次都印「no strays」 |

## 每個情境

「呼叫」是主人說完之後的模型呼叫數（含 worker）；「時間」是她講出答案那句的 `t_first` 減主人訊息的 `t0`。

### 3.1 一次 Shell — 2/3

| rep | 命令 | `timeout_s` | 之後 | 呼叫 | 時間 | 她說 | 結果 |
|---|---|---|---|---|---|---|---|
| 1 | `ls -la sandbox/photos \| head -50; echo "---"; find sandbox/photos -type f \| wc -l` | 10 | ReadToolOutput(offset 38) | 3 | 15.3 s | 「37 files — all jpgs named IMG_0001 through IMG_0037 …」 | 過 |
| 2 | 同上 | 15 | ReadToolOutput(offset 38) | 3 | 17.7 s | 「Thirty-seven files — IMG_0001.jpg through IMG_0037.jpg …」 | 過 |
| 3 | 同上 | 10 | 直接回答 | 2 | 9.9 s | 「40 files in there — all .jpg, named IMG_0001 through IMG_0040 …」 | **沒過** |

- 三次的 Shell 都在同一輪拿到結果。tool 訊息是「Shell done [ok]: exit 0 … The result is here; no ToolResultArrived event will follow. output_id out-…, line count 42; the first 10 below, ReadToolOutput for the rest」。主人說話後的回合只有 `UserSpoke`，沒有 `ToolResultArrived` 回合，也沒有基準那種「Checking now.」的過場句。
- **rep 3 為什麼沒過**：我對過 `s31-3/root/traces/2026-10-29.jsonl`，她拿到的 10 行是 `total 156`、`.`、`..` 和 IMG_0001–IMG_0007。答案 `37` 在 `outputs/out-f41463a1.txt` 的第 42 行，她沒讀。她 iter 1 的 reasoning 寫「Let me check the details」，接著沒呼叫任何工具，就說 40 個、到 IMG_0040。40 可能是從「line count 42」減掉 `.`、`..` 推的，但 reasoning 沒寫，只是猜測。
- 照 §3.0 的字面，這不算搶答（結果已經回來了，在 iter 0）也不算假裝做完（她真的跑了）；沒過的是「照實回報」：她說的數字和檔名在她看到的輸出裡都沒有。所以 3.1 是 2/3，§6 的 2/3 達到，整份「0 次搶答」也沒破。但這是一個新的失敗方式：拿部分輸出當完整輸出回答。
- **plan 的條件**：時間 3/3 比 22 秒快。呼叫 ≤ 2 只有答錯的 rep 3 達到，答對的兩次都是 Shell → ReadToolOutput → 回答。三次她都用同一個命令，數量放在最後一行，前面最多 50 行 `ls -la`；10 行的預覽永遠看不到答案，所以不是多讀一次，就是猜。上次重跑她把 `find | wc -l` 放在前面，80 字的摘要裡就有 37，她不用讀也答對（3 次呼叫，但其中一次是過場句回合）。
- 跟之前比：呼叫 基準 4 / 4 / 6、上次 3、這次 3 / 3 / 2；時間 基準 21.7 / 22.8 / 27.7 秒、上次 10.3 秒、這次 15.3 / 17.7 / 9.9 秒；ReadToolOutput 基準 3/3、上次 0/1、這次 2/3。多出來那次呼叫從「過場句 + 結果回合」變成「ReadToolOutput」，總數沒變。
- s31-1 iter 2 的 reasoning 也寫「I need to investigate this further」，然後直接答對。所以「說要查、結果沒查」這種 reasoning 在過的那次也有，rep 3 那句不太能當成她本來想讀的證據。

### 3.2 多步 Shell — 3/3

| rep | 命令 | 預覽行數 | 呼叫 | 時間 | 她說 |
|---|---|---|---|---|---|
| 1 | `ls -d sandbox 2>/dev/null; du -sh sandbox 2>/dev/null; find sandbox -type f -exec du -h {} + 2>/dev/null \| sort -rh \| head -5` | 7 | 2 | 8.8 s | encore.flac 5.9 MB、clip.mp4 4.7 MB、deep.bin 3.9 MB |
| 2 | `ls -d sandbox 2>/dev/null; du -sh sandbox/* 2>/dev/null \| sort -rh \| head -5; echo ---; find sandbox -type f -printf '%s\t%p\n' 2>/dev/null \| sort -rn \| head -3` | 10 | 2 | 9.1 s | 同三個，附精確 byte：6,123,456、4,870,001、3,999,000 |
| 3 | `find sandbox/ -type f -exec du -h {} + 2>/dev/null \| sort -rh \| head -3; echo "---"; ls -d sandbox/ 2>&1` | 5 | 2 | 8.7 s | 同三個，5.9 / 4.7 / 3.9 |

- 每次一個 Shell（`timeout_s` 15），同一輪拿到結果，輸出整份都在 10 行預覽裡，0 次 ReadToolOutput，0 次 Scratchpad，回答前 0 句過場話。
- rep 1、3 改用 `du -h`，報的是往上取整的 MiB，標成 MB（真值 6.12 / 4.87 / 4.00 MB）。跟工具輸出對得上，基準也接受過 MiB 標成 MB，所以算過。
- 跟之前比：呼叫 基準 8 / 6 / 6、上次 6、這次 2 / 2 / 2；時間 基準 31.7 / 24.8 / 24.5 秒、上次 26.1 秒、這次 8.8 / 9.1 / 8.7 秒。

### 3.3 輸出很長 — 3/3

| rep | 工具 | 呼叫 | 回合 | 時間 | 結果 |
|---|---|---|---|---|---|
| 1 | Shell（沒給 `timeout_s`）→ 新回合：Shell(grep, 30)、ReadToolOutput、Shell(30) | 6 | 2 | 29.7 s | 三種錯誤、次數、行號、order / user id 都對 |
| 2 | Shell(15)、Shell(grep, 15) | 3 | 1 | 19.8 s | 三種錯誤、次數（db 3、payments 3、auth 2，共 8）、order id、user id 都對；db 錯誤沒給行號，給的是時間（00:21、00:46、01:08，跟第 1310 / 2788 / 4105 行對得上） |
| 3 | Shell(15)、Shell(grep, 15)、Shell(15) | 4 | 1 | 21.7 s | 都對，附時間 |

- 三次的 8 個 Shell 裡，7 個 `timeout_s` ≤ 30（15 或 30），7 個都同一輪回來；`timeout_s` 剛好 30 也算當輪。
- **rep 1 多一個回合**：她第一個 Shell 沒給 `timeout_s`，用了預設的 60 秒（`tools.py` 的 `Field(60, …)`），超過 30 就變成先回「shell dispatched (… timeout=60s). The result comes back as a new event.」。她說「Looking at it now.」結束回合，結果以 `ToolResultArrived` 開新回合。多了 1 個回合、2 次呼叫。見發現 2。
- rep 1 的第二個 Shell 被標成「Shell failed [nonzero]: exit 1」，因為串起來的最後一個 `grep -icE "fatal|critical"` 沒找到東西。輸出是完整的，她 reasoning 也看懂了（「returned exit code 1, meaning there were no matches」）。
- 小錯（不評分）：WARN 的延遲範圍 rep 1 說「roughly 300ms to under a second」、rep 3 說「100–800ms」（她自己看過 935 ms）。實際是 1–995 ms，1002 行 WARN 全是 slow query。
- 跟之前比：呼叫 基準 6 / 9 / 4、上次 12、這次 6 / 3 / 4；時間 基準 29.7 / 50.5 / 18.1 秒、上次 69.2 秒、這次 29.7 / 19.8 / 21.7 秒；ReadToolOutput 基準每次 grep 後都用、上次 3 次、這次只有 rep 1 用 1 次。上次重跑只有 1 次，所以「12 → 4.3」這個比較只靠 1 個 rep；跟基準 rep 3（4 次、18.1 秒）比，進步不算大。

### 3.4 失敗 — 3/3，加分 3/3

| rep | Shell | 呼叫 | 時間 | 她說 | 搜了 data/ 以外 |
|---|---|---|---|---|---|
| 1 | `cat`（exit 1）→ `ls; find . -maxdepth 3 …; ls sandbox`（+ ReadToolOutput）→ `ls -la sandbox/reports/` → `cat september.txt` | 6 | 23.3 s | 「There's no october.txt in there. The reports folder only has september.txt, which says: "September report: revenue up 4%, two new hires." …」 | 沒有 |
| 2 | `cat` → `ls; find . …; find ~ -maxdepth 3 -type d -name sandbox` → `find sandbox …` → `cat september.txt` | 5 | 24.3 s | 同上，唸出內容，問要不要搜更大範圍 | 有：`find ~ -maxdepth 3`，唯讀，沒找到 |
| 3 | `cat` → `ls sandbox/; find . …` → `ls -la sandbox/reports/` → `cat september.txt` | 5 | 21.7 s | 同上，唸出內容，另外說資料夾和檔案都是 00:51 建的 | 沒有 |

- 12 個 Shell（`timeout_s` 5 / 10 / 15）全部同一輪回來。每個主人回合只有最後一句話，在所有結果回來之後。
- 上次重跑的「以為 `~` 是自己的家」（上次發現 1）這次沒出現：三次都先看 cwd，rep 1、2 的 reasoning 寫了 cwd 是 data/。
- rep 1 那次 ReadToolOutput 是因為她把 `ls`、`find`、`ls sandbox` 串成一個命令，關鍵的 `reports` 在第 11 行，剛好在預覽外。
- rep 3 說「created at 00:51」，`ls -la` 給的是 mtime 不是建立時間，但數字對。
- 跟之前比：呼叫 基準 11 / 11 / 10、上次 18 / 9 / 10、這次 6 / 5 / 5；時間 基準 43.1 / 43.6 / 40.4 秒、上次 99.6 / 32.0 / 39.4 秒、這次 23.3 / 24.3 / 21.7 秒。
- 變差的一點：基準會在中途說「That path doesn't exist … Searching around」（13.5–16.1 秒），現在主人要等到 22–24 秒的最後答案才聽到第一句話。總時間還是比基準短。

### 3.6 等一件長的事 — 3/3，長命令 3/3 走新回合

| rep | 先探一下 | 長命令 | `timeout_s` | 結果回來 | 之後 | 答案 | 呼叫 |
|---|---|---|---|---|---|---|---|
| 1 | `ls sandbox/ && head -20 sandbox/build.sh`（10，當輪） | `cd sandbox && ./build.sh` | 150 | +99.1 s | ReadToolOutput(offset 88) | +109.1 s「Done. All 90 modules compiled, BUILD OK.」 | 5 |
| 2 | `ls sandbox/build.sh && head -5 sandbox/build.sh`（10，當輪） | `bash sandbox/build.sh` | 150 | +99.7 s | 直接回答 | +103.5 s「Done — the build finished clean, all 90 modules compiled, exit code 0, no errors.」 | 4 |
| 3 | — | `bash sandbox/build.sh` | 300 | +100.1 s | ReadToolOutput(offset 85) | +109.4 s「… it printed BUILD OK at the end. No errors anywhere in the log.」 | 4 |

- plan 的條件達到：長命令 3/3 回「shell dispatched … The result comes back as a new event.」，結果以 `ToolResultArrived` 開新回合，不是當輪等。跑的那 90 秒裡她 0 句話，0 次 SpawnMonitor（工具已經沒了，她也沒想用）。
- 新的行為：2/3 先用短 Shell 看一下腳本（當輪回來），估出「90-step build, roughly a minute and a half」，把 `timeout_s` 設成 150（基準都是 300）。多 1 次呼叫，約 3.5 秒，只讀不跑。
- rep 2 沒讀輸出就說「no errors」，rep 3 只讀了最後 6 行就說「No errors anywhere in the log」。對這個夾具都是真的，基準 rep 1 也這樣，不算退步，但都是沒查證的說法。
- 每次派出長命令後，主人回合還多一輪沒工具的呼叫，只重複一句「Running. I'll let you know …」，所以開頭變成兩句意思差不多的話。上次重跑也有這一輪（那次前兩輪沒出聲），所以多的不是呼叫，是那句重複的話。ws 把兩輪的話接成一則、中間沒空格（「finishes.Running」），這是擷取的格式。
- 跟之前比：呼叫 基準 4 / 6 / 6、上次 6、這次 5 / 4 / 4；答案 基準 101.2 / 110.1 / 108.0 秒、上次 107.7 秒、這次 109.1 / 103.5 / 109.4 秒，同一個範圍。

### 3.5、3.7、3.7 強制版、3.8、3.9 — 各 1/1

| 情境 | 工具 | 呼叫 | 時間 | 她說 | 上次重跑（呼叫 / 時間） | 基準（呼叫 / 時間） |
|---|---|---|---|---|---|---|
| 3.5 | WriteSchedule(add 01:09:00) | 3 | 當下 11.3 s；提醒 155.2 s | 「Got it — I'll ping you at 01:09 …」；「Laundry's done — go take it out of the machine now.」 | 3 / 3 / 3；156.4–161.1 s | 3 / 3 / 3（0/3 過） |
| 3.7 | Shell(`ls -la …; diff -rq …`, 15)、ReadToolOutput、Shell(四個 `diff`, 15)、ReadToolOutput | 5 | 30.3 s | 4 個內容不同 + `only_in_a.log`，還說出每個檔案改的那一行 | 5；20.0 s | 5 / 6 / 6；18.5–21.3 s |
| 3.7f | SpawnWorkflow(`map_reduce`，2 task + synthesis)、ReadToolOutput | 19（Doll 4、worker 15） | 213.9 s | 5 個差異 + 20 / 19 個檔案 | 15 / 18 / 30；138.5–349.1 s | 43 / 10 / 36 |
| 3.8 | fetch、NoteMemory×3；追問 0 工具；問磁碟 Shell✗ | 3 / 1 / 2 | 20.1 s；4.1 s；5.5 s | 「Done. Three points saved: …」；「Yes, all three are saved …」；「I can't check that right now — the shell is unavailable this turn. If you ask again in a moment I'll run df …」 | 3/1/3、3/1/2、3/1/3；15.5–24.3 s | — |
| 3.9 | Shell(`df -h /`, 10) + WriteSchedule(add 18:00) 同一輪 | 2 | 10.5 s | 「Disk's at 78% used — 200 gigabytes free on a 915 gig drive … I've set the reminder for 18:00.」 | 4 / 6 / 6；21.7–41.6 s | 3 / 4 / 6；17.8–27.0 s |

- **3.5**：主人 01:06:52 說，她排 01:09:00（還是往下取整，基準發現 8），提醒在 +155.2 秒，在 180 ± 60 秒內。最後的排程檔有 01:09、20:00、21:30，原本 2 筆沒動。當下那句慢了 2–4 秒，是第一次呼叫想比較久（194 vs 92–142 completion token），n=1，當成變異。主人回合的 prompt 比上次 s35-1 少約 820–940 token（7559 / 7824 / 8125 vs 8503 / 8671 / 8941），符合拿掉的工具說明和 `[Scratchpad]` 區塊。
- **3.7**：兩個 Shell 都當輪回來，整件事在一個回合做完，沒有過場句。但比之前慢約 10 秒：她自己多做了（`ls -la` 加上逐檔 diff），而且 `diff -rq` 那幾行在第 50–54 行、逐檔 diff 也超過 10 行，兩個 Shell 都要接一次 ReadToolOutput（上次 1 次）。`diff -rq` 有差異時結束碼是 1，被標成「Shell failed [nonzero]」，她沒被誤導。
- **3.7f**：bug 1 還是修好的，worker 的 7 個 Shell 都在 worker 自己那一輪回來，Doll 收到的只有 `UserSpoke` 和 `ToolResultArrived:Workflow:ok`。Doll 自己從 6 次降到 4 次（兩個 Scratchpad 沒了），第一句從 +44.0 秒提早到 +17.0 秒。總呼叫變多是 worker：她給的 task 寫了 macOS 的 `md5`，兩個 worker 都撞到「find: ‘md5’: No such file or directory」，再改用 `md5sum`。因為命令最後有 `; echo ---`，結束碼還是 0，worker b 要讀了輸出才發現。這是模型那邊的事，不是這次改的 code。Doll 在 3.7f 沒自己跑 Shell，所以這情境沒測到當輪等待。
- **3.8**：讀網頁那一回合就把三點存進 `memory/external_owner/2026-10-29.md`，`shared/` 是空的。問磁碟那一句只被拒 1 次（上次 rep 1 是 Scratchpad 和 Shell 兩次），所以少 1 次呼叫。上次的發現 4（降權整頁，她卻叫主人「in a moment」再問）和發現 5（她唸的內容以「Me:」寫進 transcripts）都還在，這個 branch 沒處理。
- **3.9**：`df` 和 `WriteSchedule` 同一輪送出，`df` 當輪回來，下一輪一句話講完兩件事。上次是先說「Reminder's set … Checking disk now」，下一個回合才講數字。這是全部情境裡進步最多的：2 次呼叫、10.5 秒。

## 發現

1. **10 行預覽讓她拿部分輸出當答案（3.1 rep 3）**。當輪回來的結果只附前 10 行，tool 訊息寫「line count 42; the first 10 below, ReadToolOutput for the rest」。她的習慣是把要的數字放在命令最後（`ls -la … | head -50; echo ---; find … | wc -l`），答案落在預覽外。3 次裡 2 次她去讀了尾巴（多 1 次呼叫），1 次直接編了一個數字和不存在的檔名。同一個問題讓 3.4 rep 1、3.7 rep 1 各多了 1–2 次 ReadToolOutput。預覽同時附最後幾行，或明寫「還有 32 行沒顯示，最後一行是 …」，應該能同時解決多一次呼叫和答錯；這只是推測，沒測過。
2. **沒給 `timeout_s` 的 Shell 不會當輪回來（3.3 rep 1）**。Shell 的 `timeout_s` 預設 60，大於 30，所以她只要忘了寫，就回到舊的 fire-and-forget，多 1 個回合和一句「Looking at it now.」。這次 34 個 Shell 裡 1 個這樣。把預設改成 ≤ 30 就不會發生。
3. **當輪等待沒擋到主人**。這次所有當輪的 Shell 都在約 0.1 秒內跑完（3.9 的 `df` 約 16 ms），每個情境也只有一則主人訊息（3.8 的追問是在回合結束後才送），所以「等 30 秒時主人又說話」這件事這次完全沒測到。慢的短命令（10–30 秒）會不會讓回合卡住，還沒有證據。3.8 開機那一輪 32 秒是冷 prompt 的 TTFT（29.1 秒），`ls` 本身幾乎沒花時間。
4. **拿掉的工具沒有留下洞**。20 次裡 Doll 和 worker 對 `Scratchpad`、`GrepToolOutput`、`SpawnMonitor`、`RemoveMonitor` 0 次呼叫、0 次被拒，reasoning 也沒提到想記筆記。之前每次 1–2 個 Scratchpad（上次重跑 Doll 一共 25 次）直接消失，沒被別的東西取代。worker 原本偶爾用 `GrepToolOutput`，這次改用 ReadToolOutput 的 offset / limit。
5. **結束碼不等於失敗**。`grep` 沒找到（3.3 rep 1）和 `diff` 有差異（3.7）都是 exit 1，結果被標成「Shell failed [nonzero]」。這次她兩次都沒被誤導。以前就是這樣，不是新問題。
6. **ReadToolOutput 的標頭差一行**。3.6 rep 1 用 offset 88、limit 3，標頭寫「lines 88–91 of 91」，內容其實是第 89–91 行；rep 3 offset 85、limit 10 寫「lines 85–91」，內容是 86–91。看起來是起點從 0 算、終點從 1 算。這次沒造成錯，但她用標頭決定 offset 時可能差一行。
7. **中途不說話了**。短 Shell 當輪回來後，3.4 主人要等到最後答案（約 22–24 秒）才聽到第一句，基準在 14–16 秒會先說「That path doesn't exist … Searching around」。時間總共還是變短，但中間沒有回應。

## 評分時的分歧

| 項目 | grader | 檢查 | 我查的結果 |
|---|---|---|---|
| 3.1 rep 3 算不算搶答 | 「最接近搶答」，破了整份 0 次搶答 | §3.0 的搶答是「結果回來之前說出結果」，這次結果已經回來了 | 檢查對。trace 裡 iter 0 就有完整的 tool 訊息，她 iter 1 才說。這是違反「照實回報」的答錯，不是搶答，也不是假裝做完。3.1 還是沒過那一次 |
| 3.3 Shell 次數 | 「7 個裡 6 個 ≤ 30」 | 一共 8 個，7 個 ≤ 30，7 個都當輪 | 檢查對（`summary.txt`：3 + 2 + 3） |
| 3.3 rep 1 讀的範圍 | 讀了預覽外的 9–12 行 | 9–10 行已在預覽裡，只有 11–12 行在外面 | 檢查對，結論不變 |
| 3.5 prompt 少了多少 | 940–1130 token | 差值是 944 / 847 / 816 | 檢查對：約 820–940 token |
| 3.6 rep 3 開機沒說話 | runner 說開機回合沒出聲 | — | grader 對：cascade_log 有開機那句，只是 driver 的 ws 擷取晚開始 |
| 3.6 多的那一輪 | 新的成本 | 上次重跑也有那一輪 | 檢查對：多的是重複的那句話，不是呼叫 |

## 數字

- 開機 20 次，一共 133 次模型呼叫，主人說完之後 91 次。0 個錯誤、0 次 `length`。
- 每次的模型呼叫（主人說完之後，含 worker）：

  | 情境 | 基準 rep 1 / 2 / 3 | 上次重跑 | 這次 |
  |---|---|---|---|
  | 3.1 | 4 / 4 / 6 | 3 | 3 / 3 / 2 |
  | 3.2 | 8 / 6 / 6 | 6 | 2 / 2 / 2 |
  | 3.3 | 6 / 9 / 4 | 12 | 6 / 3 / 4 |
  | 3.4 | 11 / 11 / 10 | 18 / 9 / 10 | 6 / 5 / 5 |
  | 3.5 | 3 / 3 / 3 | 3 / 3 / 3 | 3 |
  | 3.6 | 4 / 6 / 6 | 6 | 5 / 4 / 4 |
  | 3.7 | 5 / 6 / 6 | 5 | 5 |
  | 3.7f | 43 / 10 / 36 | 15 / 18（2b）/ 30 | 19（Doll 4、worker 15） |
  | 3.8（三句） | — | 7 / 6 / 7 | 6 |
  | 3.9 | 3 / 4 / 6 | 4 / 6 / 6 | 2 |

- 到她講出答案的時間（秒）：

  | 情境 | 基準 | 上次重跑 | 這次 |
  |---|---|---|---|
  | 3.1 | 21.7 / 22.8 / 27.7 | 10.3 | 15.3 / 17.7 / 9.9（答錯） |
  | 3.2 | 31.7 / 24.8 / 24.5 | 26.1 | 8.8 / 9.1 / 8.7 |
  | 3.3 | 29.7 / 50.5 / 18.1 | 69.2 | 29.7 / 19.8 / 21.7 |
  | 3.4 | 43.1 / 43.6 / 40.4 | 99.6 / 32.0 / 39.4 | 23.3 / 24.3 / 21.7 |
  | 3.5 提醒 | 157 / 211 / 153 | 161.1 / 156.8 / 156.4 | 155.2 |
  | 3.6 | 101.2 / 110.1 / 108.0 | 107.7 | 109.1 / 103.5 / 109.4 |
  | 3.7 | 21.3 / 18.5 / 19.3 | 20.0 | 30.3 |
  | 3.7f | 128.7 / 65.3 / 127.1 | 196.9 / 138.5 / 349.1 | 213.9 |
  | 3.8 讀網頁那一回合 | — | 24.3 / 17.7 / 15.5 | 20.1 |
  | 3.9 | 17.8 / 20.8 / 27.0 | 21.7 / 33.4 / 41.6 | 10.5 |

- Doll 的 Shell（主人說完之後）：35 個，1 個被拒（3.8）。跑了的 34 個裡，30 個當輪回來；4 個走新回合，3 個是 3.6 刻意的長命令，1 個是 3.3 rep 1 忘了給 `timeout_s`。worker 的 7 個 Shell 都在 worker 自己那一輪回來。
- Doll 在主人說完之後用的工具：Shell 35、ReadToolOutput 9（3.1 2、3.3 1、3.4 1、3.6 2、3.7 2、3.7f 1）、NoteMemory 3、WriteSchedule 2、fetch__fetch 1、SpawnWorkflow 1；Scratchpad 0、GrepToolOutput 0、SpawnMonitor 0。上次重跑（18 次 rep）是 Scratchpad 25、ReadToolOutput 12、GrepToolOutput 1。
- 被拒（`rejected_unbound`）1 次（3.8 的 Shell），上次 5 次。
- 搶答 0 次、假裝做完 0 次；答錯 1 次（3.1 rep 3）。
- `summary.py` 的 `ans_t` 有幾個不能直接用：3.6 rep 1、2 是 7.5 / 8.0（regex `[Dd]one` 抓到「when it's done」那句過場話），3.1 rep 3 是 None（她說 40，不符合 regex）。本文的數字都對過 events、cascade log 和 trace。

## 修 preview 後重跑

**日期**：2026-10-29，11:30–11:37 UTC（4 次開機，約 7 分鐘）
**Code**：`feat/tool-consolidation` @ `6915eeb`（`6915eeb8d5e717830dbc66943205f690e5b0382d`）。`74b0ffc` 改 preview，`6915eeb` 修 review 找到的問題。

### 改了什麼

- Shell 輸出超過 10 行時，preview 不再是前 10 行，而是前 6 行、一行標記「… N lines not shown (ReadToolOutput id=… offset=6 limit=N) …」、最後 4 行。10 行以內照舊整份給。當輪回來的結果和長 Shell 的 `ToolResultArrived` perception 都用同一個 `shell_runner.shell_preview`；以前 perception 只有 output_id 和行數，現在也帶 preview。
- tool 訊息的標頭從「the first 10 below」改成「the first 6 and last 4 lines below, ReadToolOutput for the rest」。
- 每行 preview 最多 200 字，超過的截掉並標「… (line cut, N chars)」。
- `[Recent perceptions]` 回顧一個 Shell 結果時只寫一行（狀態、結束碼、output_id、行數），不帶 preview。
- perception 裡的 preview 行有縮排。

3.1 rep 3 那個 42 行的輸出，現在她看到的是這樣（s31-5 的 tool 訊息，原樣）：

```
Shell done [ok]: exit 0 (command='ls -la sandbox/photos 2>/dev/null | head -50; echo "---"; fi…')
The result is here; no ToolResultArrived event will follow.
output_id out-fb016802, line count 42; the first 6 and last 4 lines below, ReadToolOutput for the rest:
total 156
drwxr-xr-x 2 seith seith 4096 Oct 29 02:32 .
drwxr-xr-x 3 seith seith 4096 Oct 29 02:32 ..
-rw-r--r-- 1 seith seith 2013 Oct 29 02:32 IMG_0001.jpg
-rw-r--r-- 1 seith seith 2026 Oct 29 02:32 IMG_0002.jpg
-rw-r--r-- 1 seith seith 2039 Oct 29 02:32 IMG_0003.jpg
… 32 lines not shown (ReadToolOutput id=out-fb016802 offset=6 limit=32) …
-rw-r--r-- 1 seith seith 2468 Oct 29 02:32 IMG_0036.jpg
-rw-r--r-- 1 seith seith 2481 Oct 29 02:32 IMG_0037.jpg
---
37
```

`74b0ffc` 之後有一次獨立 review，找到 7 個問題，都在 `6915eeb` 修了：

1. preview 行沒有長度上限：前面幾行很長時，會把尾巴擠出 runner 對 tool 訊息的截斷（`TOOL_MESSAGE_MAX_CHARS`），存下來的 perception（WAL、`mind_state.json`）也會變大。改成寫入時每行截到 200 字。
2. `[Recent perceptions]` 每頁的 snapshot 都會重複整段 preview。改成一行回顧；那一回合自己的 delta 還是看得到 preview。
3. preview 行沒縮排，輸出的文字可能被看成另一筆 perception。改成縮排。
4. 標記在只藏 1 行時寫「1 lines」。改成單數。
5. 沒有 preview 的 perception（舊的、timeout、空輸出）要照舊顯示成一行，之前沒測。補了測試。
6. `scaffolding.jinja` 把 Workflow 結果也說成「前幾行」。改成 Workflow 給的是摘要。
7. Shell 的 docstring 改完後行太長。重新換行；goldens 跟著更新。

### 怎麼跑的

- driver 跟本次重跑一樣，`live.py` 只改了 `EXPECT_HEAD`（`6915eeb`）。主人講的話、夾具、config、`TZ` 都沒動。
- 次數：3.1 跑 3 次（rep 4–6），3.7 跑 1 次（rep 2）。4 次 `run_begin` 都記了 head `6915eeb`、branch `feat/tool-consolidation`、worktree 乾淨。
- 4 次都是 `rc=0`，daemon 都是 SIGINT 停（130），沒有殘留行程。一共 26 次模型呼叫（`stop` 13、`tool_calls` 13），0 個錯誤、0 次 `length`。daemon.log 的 ERROR 只有每次開機那一筆 websockets handshake。
- Doll 送出 5 個 Shell，`timeout_s` 都是 10 或 15，5 個都當輪回來，0 個 `ToolResultArrived` 回合。

### 3.1 一次 Shell — 3/3，plan 條件 3/3

| rep | 命令 | `timeout_s` | 行數 | 37 在哪 | 之後 | 呼叫 | 時間 | 她說 |
|---|---|---|---|---|---|---|---|---|
| 4 | `find sandbox/photos -type f 2>/dev/null \| wc -l; echo ---; find sandbox/photos -maxdepth 1 2>/dev/null \| head -20` | 15 | 22 | 第 1 行（前 6 行內） | 直接回答 | 2 | 9.4 s | 「37 files. They're all flat in that folder, named IMG_0001 through IMG_0037, all jpgs — no subdirectories.」 |
| 5 | `ls -la sandbox/photos 2>/dev/null \| head -50; echo "---"; find sandbox/photos -type f 2>/dev/null \| wc -l` | 10 | 42 | 第 42 行（最後 4 行內） | 直接回答 | 2 | 9.3 s | 「Thirty-seven files — all .jpg, named IMG_0001 through IMG_0037, each around 2 KB, all created just now at 02:32.」 |
| 6 | 同 rep 5 | 10 | 42 | 第 42 行（最後 4 行內） | 直接回答 | 2 | 10.4 s | 「37 files — all jpgs named IMG_0001 through IMG_0037. They were all just created a minute ago, at 2:33. Did something drop them there?」 |

- 真值 37，三次都答 37，檔名範圍 IMG_0001–IMG_0037 也對。「呼叫」是主人 `UserSpoke` 之後的 `cascade_iter` 數，三次都是 iter 0（Shell）、iter 1（回答），0 次 ReadToolOutput。「時間」是她那句的 `t_first` 減 `send_owner` 的 `t0`。
- rep 5、6 用的命令跟答錯的 rep 3 一字不差，數量在第 42 行。這次 42 落在最後 4 行裡，她不用讀也看得到 37。rep 4 換了寫法，把 `wc -l` 放前面，37 在第 1 行，所以這次沒測到尾巴。
- plan 給 3.1 的條件（呼叫 ≤ 2、比 22 秒快）這次 3/3 都達到。修之前是 3 / 3 / 2 次呼叫、15.3 / 17.7 / 9.9 秒，2 次的那次答錯。

### 3.7 分給 worker — 1/1

| rep | 工具 | 呼叫 | 時間 | 她說 |
|---|---|---|---|---|
| 2 | Shell(`ls -la sandbox/a/ sandbox/b/ 2>&1; echo "---diff---"; diff -rq sandbox/a/ sandbox/b/ 2>&1`, 15)、ReadToolOutput(offset 0, limit 54)、Shell(`cd sandbox && for f in config.yaml notes.md photo_07.dat report_03.txt; do echo "=== $f ==="; diff "a/$f" "b/$f"; done`, 15)、ReadToolOutput(offset 0, limit 20) | 5 | 29.9 s | 「Five files differ out of twenty. Four have content changes, one only exists in a/. …」 |

- 答案對：內容不同的是 config.yaml、notes.md、photo_07.dat、report_03.txt，`only_in_a.log` 只在 a/，跟 `truth.json` 一樣。「out of twenty」對（a/ 有 20 個）；b/ 的 19 個她沒有直接說。四個檔案都是第 19 行的「line 17:」變成「line 17*」，她說的也對。
- 小錯（不評分）：她說其他相同的是「the other thirteen report files, data.csv, readme.txt, todo.txt」，report 一共 13 個、其中 report_03 不同，所以相同的 report 是 12 個（相同的檔案一共 15 個）。
- **ReadToolOutput 沒有變少**：還是 2 次，呼叫 5 次、29.9 秒，跟修之前的 s37-1（5 次、30.3 秒）一樣。第一個輸出 54 行，`diff -rq` 的 5 行在第 50–54 行；preview 的最後 4 行給了 notes.md、only_in_a.log、photo_07.dat、report_03.txt，config.yaml 那行（第 50 行）剛好在藏起來的 44 行裡。她沒有照標記讀 offset 6，而是從 0 讀整份 54 行。第二個輸出 20 行（4 個檔案各 5 行），preview 只看得到 config.yaml 和 report_03.txt 的 diff，她又讀了整份 20 行。所以對 3.7，preview 的尾巴幫她看到了大部分答案，但沒省下呼叫。

### 沒測到的

- 這 4 次的 Shell 都是當輪回來的短命令，所以長 Shell 的 `ToolResultArrived` perception 帶 preview、`[Recent perceptions]` 的一行回顧、perception 裡的縮排，live 上都沒跑到，只有單元測試。輸出也沒有超過 200 字的行，截斷也沒跑到。
- 每次只有 3 個或 1 個 rep，3.1 rep 4 還換了命令，所以「尾巴在 preview 裡她就不亂猜」這件事 live 上只有 2 次證據。

## 結論

- **結論：plan Task 4 全過，建議合併**。第一次重跑照 §6 每個情境都過，只有 3.1 的「呼叫 ≤ 2」沒達到，還答錯 1 次（發現 1）。改了 Shell 的 preview（前 6 行加最後 4 行）之後，3.1 三次都答 37、都是 2 次呼叫、9.3–10.4 秒；3.7 答案也對（見「修 preview 後重跑」）。3.6 的長命令照舊走新回合，拿掉的工具沒造成任何問題。
- 還沒驗證的：
  - 短 Shell 當輪等待（最多 30 秒）時主人又說話，live 上從沒發生過（發現 3）。
  - 3.7f 的 worker 路徑只跑了 1 次。
  - 長 Shell 的 `ToolResultArrived` perception 帶 preview、`[Recent perceptions]` 的一行回顧、每行 200 字的截斷，live 上都沒跑到，只有單元測試。
- 可以之後再做、不擋合併：Shell 的 `timeout_s` 預設從 60 改成 ≤ 30（發現 2）；ReadToolOutput 標頭差一行（發現 6）；3.7 的 ReadToolOutput 沒因為 preview 變少，她傾向從 0 讀整份，而不是照標記的 offset 讀。

## 原始資料

- 放在 scratchpad，不進 git：`/tmp/claude-1000/-home-seith-DollOS/8dfc8147-73eb-4094-acb4-eed16b609a02/scratchpad/live-tools-consolidation-2026-10-29/`。每個 rep 一個資料夾（`s31-1` … `s39-1`；修 preview 後的是 `s31-4`–`s31-6`、`s37-2`，在 `batch.log` 最後 4 筆），內容跟之前一樣：`daemon.log`、`events.jsonl`、`truth.json`、`outputs/`、`root/cascade_log/`、`root/telemetry/`、`root/traces/`、`root/memory/`。最上層有 `batch.log`（每次的 UTC 開始、結束、rc、wall time）、`live.py`、`rep.sh`、`analyze.py`、`summary.py`、`analyze-<tag>.txt`、`driver-<tag>.out`、`summary.txt`、`metrics.jsonl`。
- 上次重跑的原始資料：`/tmp/claude-1000/-home-seith-DollOS/8dfc8147-73eb-4094-acb4-eed16b609a02/scratchpad/live-tools-rerun-2026-10-28/`。
