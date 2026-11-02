# 交接：現況報告（2026-11-02，從 zenith 搬到 Mac）

給接手的 Claude Code。先讀 `CLAUDE.md`，再讀這份。

## 一句話

v1 的功能都做完、合併進 `main` 了，只差**正式驗收**：照 spec 跑 3 次，算分，寫報告。試跑過了，結果不錯；正式的 3 次還沒有一次成功跑完。

## v1 是什麼

使用者的定義（2026-10-27）：Doll 用 Qwen3.8（Strata）聊天，記得主人糾正過的事，會用工具把事做完。只支援 macOS。v1 完成之前，macOS 移植、桌寵 UI、4B steering、Mimir/SNN、TTS 調校都凍結。

## 已經做完的（都在 `main`，已推）

- Harness S1–S7、記住糾正、工具驗收擋路的 4 個 bug、工具合併：見 `CLAUDE.md` 的「已完成」表。
- v1 收尾第一部分的小修（commit `cd7faf2` 到 `4dc05ce`）：
  - Shell 長輸出預覽改成前 6 行加後 4 行。
  - 舞台旁白過濾。
  - keeper 讀的對話紀錄尾巴從 8000 字加到 40000 字。
  - 其他小修。
- v1 驗收 spec：`docs/superpowers/specs/2026-10-29-v1-end-to-end-acceptance-design.md`。用虛擬時鐘，最後一次修改是 `bf94212`。

## 現在卡在哪：v1 正式驗收

### 怎麼驗

照 spec 演一個完整的「兩天」：
- 第一天晚上：A1–A-bye，包含閒聊翻頁、用工具、讀網頁降權。
- 重開 daemon。
- 第二天早上：B1–B10。
- 最後等每天的整理和 profile keeper 跑完。

一次大約 30 分鐘，要用真的 Strata。

**過關條件**：
- 每一步在 3 次裡至少過 2 次。
- 每一次都要重開成功。
- §4.0 的四條零容忍：不搶答、不假裝做完、不唸舞台旁白、不當機。

### 工具

工具放在 `scripts/v1_acceptance/`。它原本在 zenith 的 /tmp，這次才搬進 repo。

| 檔案 | 作用 |
|---|---|
| `run.sh <tag>` | 跑一次，接著評分，最後清掉殘留行程（當掉後也會清） |
| `live_v1.py` | driver |
| `grade_v1.py <tag>` | 評分；`--summary <tag>...` 合併多次 |
| `fixtures_v1.py`、`chat_pool.py` | 夾具和 80 則閒聊 |
| `v1lib.py` | 共用函式，含 VClock |
| `vclock_launch.py` | 用 time-machine 讓 daemon 跑在虛擬時間 |
| `stub_llm.py` | 乾跑用的假模型 |
| `test_grade_synth.py` | 評分器自我測試，32 項 |
| `pilot-grade.txt` | 試跑的完整成績 |

**在 Mac 上要先做的**：
1. 驗收必須跑在乾淨、detached 在 `4dc05ce` 的 worktree 上（driver 的 preflight 會檢查 HEAD）：

   ```bash
   git worktree add --detach .worktrees/v1-accept 4dc05ce
   cd .worktrees/v1-accept && uv sync
   ```

2. 設定環境變數 `DOLLOS_V1_WT`（worktree 的絕對路徑），需要時再設 `DOLLOS_V1_STRATA`，預設是 `http://100.110.158.36:8080`。
3. 從 `run.sh` 所在的目錄跑；輸出會寫在旁邊的 `<tag>/`。先用 `stub_llm.py` 乾跑一次，確認 Mac 上路徑、port、`uvx mcp-server-fetch` 都正常。
4. 這些腳本只在 Linux 上跑過。Mac 上可能卡的地方：
   - `pgrep` 的寫法、`/proc/<pid>/cwd`（`run.sh` 找殘留行程時會用到）。
   - process group。
   - time-machine。

   遇到就修腳本，不要改 worktree。

### 試跑結果（不算分，在 zenith 上用真的 Strata）

完整成績在 `scripts/v1_acceptance/pilot-grade.txt`。

- **過的**：A4、A5、A6、A10–A11（主人插話）、A12、翻頁（23 則閒聊後翻頁，翻完還記得貓叫 Miso）、A-probe、A-fetch、A-taint、重開、B-probe、B4、B5–B6、偏好第二天（10/10）、每天整理（keeper 把「貓叫 Miso」補進 profile）。
- **四條零容忍**全過。
- **沒過 2 項**：
  - **偏好第一天**：24/31，77%，門檻 80%。
  - **B3**：問昨晚的備份，她答「3 個 snapshot、都正常」，沒說錯，但講不出 snap-0412 和 41.2 GB。用了 3 次 `Recall` 也沒找到細節。
- **沒列入評分、但像是真問題**：幾乎每則閒聊她都會叫一次 `NoteMemory`（29 則裡 26 則）。每回合因此多一次模型呼叫，開口從大約 3 秒變大約 10 秒，記憶也會塞滿瑣事。

### 正式 3 次的狀況

- **第一次跑**：s1 進行到 C18 時，**driver 自己**（不是 daemon）在 Python 3.13 裡 general protection fault。它沒清掉 daemon，s2、s3 就因為 port 被占而開不起來。`run.sh` 已經補上自動清殘留。
- **重跑**：剛開始就被停掉了，一筆也沒跑完。
- **zenith 上的 Python 當機**：一共 4 次，全在 uv 管理的 Python 3.13（測試腳本 2 次、評分程式 1 次、10/28 daemon 1 次）。原因沒查出來，**不要**下結論說是硬體問題，使用者說另一個專案的 session 沒事。到 Mac 上先看會不會再發生。

### 下一步

1. 在 Mac 上照上面準備好，乾跑一次。
2. 問使用者 OK 後，跑正式 3 次（s1、s2、s3），每次大約 30 分鐘。
3. `grade_v1.py --summary s1 s2 s3`，看每一步是否 ≥2/3。spec 的「需要主人看」那幾項給使用者看。
4. 寫驗收報告到 `docs/research/`，請使用者判定 v1 是否完成，再更新 `CLAUDE.md` 的「下一個」。
5. 看 3 次的數據之後，再決定要不要修：每回合都叫 `NoteMemory`、B3 記不住細節、偏好第一天剛好卡在門檻。

## 之後（v1 完成後）

- **macOS 移植**：spec `docs/superpowers/specs/2026-10-21-macos-port-design.md`，plan `docs/superpowers/plans/2026-10-21-macos-port.md`。現在 workspace 已經在 Mac 上了，可能會提早。
- **自主活動**：讓 agenda 回合在沙盒裡真的動手，加防護。使用者選了先收 v1，還沒寫 spec。
- 頁上降權之後，到換頁前都不能用 Shell 等工具，代價還沒評估。

## 使用者偏好（zenith 上的記憶，Mac 上沒有，記得存進去）

- 用繁體中文，**短**，一次只問一個決定。長、多主題的回覆會讓使用者負荷過重。
- 派實作 subagent 之前，先跟使用者說這個 task 要做什麼，等 OK。
- 合併要使用者同意，用 fast-forward。**不要推**，除非使用者叫你推。
- 不要碰使用者的未追蹤檔。
- Strata（`100.110.158.36:8080`，只有一個 slot）可以隨便用，不用問。
- 先 profile、先加速，再開長跑。
- 話不要說太滿：沒查證的原因不要講成結論。
- 人設方向：大肥魚風格，客服腔越少越好，可以犯可原諒的錯，但要真的成長（`docs/superpowers/specs/2026-10-25-base-persona-template-design.md`）。

## 其他 repo（都已推，跟 GitHub 同步）

- `dollm`、`fish-tts`、`luxtts-onnx`（`feat/apple-silicon` 在另一個 worktree）、`piper-finetune`、`tuna`。
- `snn-vlm`：工作分支是 `moe-columns`。研究用，不在 v1 路徑上。

**沒進 git、要另外搬的**：
- `dollm-flybrain/`（536 GB）
- `DollOS/experiments/steer-think/`（5.5 GB）和 `docs/research/2026-10-26-steer-thinking-voice-qwen35-4b.md`（使用者的未追蹤檔）
- `snn-vlm/strata_yesno.py`
- `_reviews/`
