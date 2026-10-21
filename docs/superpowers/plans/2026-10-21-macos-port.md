# DollOS 搬到 Mac：macOS 移植 plan

**Spec**：`docs/superpowers/specs/2026-10-21-macos-port-design.md`（APPROVED 2026-10-21，同時核可兩個 redesign R1a 決定：Mac 上 system pulse 關掉；每天使用前取消註冊 Shell／SpawnWorkflow／SpawnMonitor）。這份 plan 不改 spec。
**日期**：2026-10-21
**狀態**：待使用者審
**分支 / worktree**：`feat/macos-port`，`.worktrees/macos-port/`，從 `main`（`0c9d144`）開。

---

## 一句話

照 spec 的 M1–M8，讓今天 main 上的 daemon 在你的 Mac 上用 `uv sync` 裝起來、交給 launchd 長期跑。前面 9 個 task 都在 zenith 上做、用 fake 測 macOS 的路徑，每個做完 Linux 都照常跑；最後一個（M8）由 Mac 上的 Claude Code 照 spec §4.2 的 19 步清單驗收。

---

## 查證後的現況（main `0c9d144`，2026-10-21）

- **spec 的 file:line 全部對過**，跟 code 一致（差 1–2 行的不列）。兩個小出入：
  - SIGTERM 的 handler 在 `kernel.py:1231-1232`；spec 引的 `__main__.py:27-31` 是收尾後回 0 的那段。結論（SIGTERM 收尾後 exit 0，KeepAlive 不重啟）不變。
  - redesign spec 寫的 `MAIN_TOOLS` 位置（`tools.py:1188-1198`）過時了，現在是 `tools.py:1280-1290`。
- **測試基準**：`uv run pytest` 在收集階段就停（`tests/voice/bridge/test_mic.py`、`test_speaker.py`：`OSError: PortAudio library not found`），跟 spec §4.1 寫的一樣。略過這兩個檔：**1804 passed、3 failed、7 skipped**（1814 個）。
- **spec 漏掉的一件事**：那 3 個 failed 是 `tests/voice/test_tts_qwen3.py`，測試函式裡 `import torch`，zenith 沒有 torch。Mac 也沒有 torch，所以清單第 9 步（「0 failed」）一定過不了。Task 1 用跟 PortAudio 一樣的方式處理（環境不支援就 skip 並寫原因）。
- **pulse 的 poller 本來就看 `enabled`**（`perception/system_pulse.py:371-374`，關掉就不跑）。真正沒擋住的是 PulseObserver（`kernel.py:1217` 只看 `alerts_enabled`）和 scaffolding 的身體描述。
- `src/` 裡今天沒有任何地方讀 `sys.platform`，所以「只有 `host.py` 讀」可以用一條 grep 測試守住。
- zenith 的 `.venv` 裡確實有自癒留下的 symlink：`sherpa_onnx/lib/libonnxruntime.so -> onnxruntime/capi/libonnxruntime.so.1.24.4`。
- `main` 現在比 `origin/main` 多 71 個 commit（spec 寫 69，之後又多了 spec 的兩個 commit）。

---

## 還沒決定的兩件事（不擋開工）

- **Q1 插電時讓 Mac 保持清醒？**（spec §7，建議「要」）還沒決定。Task 0–9 不管答案都一樣：睡眠偵測和「錯過的日記、提醒」的紀錄（Task 6），兩種答案都需要。
  - 答「要」：做 Task 6b（`[host] keep_awake`，預設關），清單第 18 步才跑。6b 只用到 Task 2 的 `host.py`／`check_host`，之後任何時候都能補。
  - 答「不要」：不做 6b、跳過第 18 步。她跟著 Mac 睡，晚上錯過的日記記成 `host_asleep`。
- **Q2 什麼時候把 main push 上 GitHub？**（spec §7，建議 Task 1 合進 main 就 push；`ningyos/DollOS` 是公開 repo）還沒決定。每個 task 做完 Linux 都照常跑、全套測試綠，所以這個分支在任何一個 task 結束時都能合進 main。答案只決定 Mac 什麼時候、從哪裡拿到 code：
  - Task 1 後就 push：Mac 先跑清單 1–8，早點發現沒料到的問題。
  - 全部做完才 push：Mac 從第 1 步一次跑到第 19 步。
  - 不 push：Mac 透過 Tailscale 從 zenith `git clone`。
  - 不管哪一種，push 之前都先問你。

---

## Tasks

每個 task 一個 subagent，TDD：先寫會失敗的測試，再改 code，最後全套測試綠。派 subagent 之前先跟你說這個 task 要做什麼，等你 OK。順序：Task 0 → 1 → 2，Task 3–7 彼此不相依（照編號做），然後 Task 8 → 9，合進 main，最後 Task 10 在 Mac 上做。

每個 task 的「解鎖」寫的是它讓 spec §4.2 清單的哪幾步能過。

### Task 0 — worktree 和基準

- `git worktree add .worktrees/macos-port -b feat/macos-port main`，然後 `uv sync`（不加任何 extra）。
- 基準：`uv run pytest -q --ignore=tests/voice/bridge/test_mic.py --ignore=tests/voice/bridge/test_speaker.py`。預期 1804 passed、3 failed（`test_tts_qwen3.py`，沒有 torch）、7 skipped。不一樣就列給你看，不在這裡修。

### Task 1 — M1 依賴（spec §3.4、§3.6、§4.1 的 PortAudio）

- 檔案：`pyproject.toml`、`uv.lock`、`src/dollos/voice/asr_sherpa.py`、`src/dollos/voice/tts_luxtts.py`、`tests/voice/bridge/test_mic.py`、`tests/voice/bridge/test_speaker.py`、`tests/voice/test_tts_qwen3.py`。
- 改法：
  - 釘 `sherpa-onnx==1.13.1` 和 `sherpa-onnx-core==1.13.1`，旁邊寫註解：兩個要一起升，不然 import 找不到 `libonnxruntime.<ver>.dylib`。
  - `gpu` extra 的 6 項加 `; sys_platform == 'linux'`；`[tool.uv]` 加 `required-environments`（darwin arm64、linux x86_64，照 spec §3.6）。
  - `uv lock`，**不加** `--upgrade`。
  - 刪掉 `_ensure_sherpa_onnx_symlink` 和它的呼叫；`asr_sherpa.py` 拿掉 `preload_cudnn()`。
  - luxtts：建 `LuxTTSOnnx` 之前，照 luxtts-onnx `inference.py:217-227` 同一條規則算出 provider，是 CUDA 才呼叫 `preload_cudnn()`（拿掉 `tts_luxtts.py:21` 的 import 時呼叫）。兩個引擎都用 INFO log 實際用的 provider。
  - `test_mic.py`、`test_speaker.py`：模組開頭 `import sounddevice` 失敗（`OSError`）就 `pytest.skip(<原因>, allow_module_level=True)`。
  - `test_tts_qwen3.py` 裡要 torch 的 3 個測試：`pytest.importorskip("torch", reason=...)`，原因寫「qwen3-tts 要 torch；預設安裝和 Mac 都沒有」。這兩種 skip 是測試環境的 skip，不是選實作，spec §2 規則 2 不適用。
  - venv：先刪掉 `.venv/lib/python3.13/site-packages/sherpa_onnx/lib/libonnxruntime.so`（自癒留下的 symlink，沒有任何套件的 RECORD 記得它），再 `uv sync`。worktree 的 `.venv` 在這個 task 做；main checkout 的 `.venv` 等合進 main 後再做一次。
- 測試：
  - `uv.lock` 只多了 `sherpa-onnx-core` 和 marker：比對 relock 前後每個套件的版本，一個都沒變。`uv lock --check` 通過。
  - `uv sync` 之後 `sherpa_onnx/lib/libonnxruntime.so` 是 core 裝的一般檔案，不是 symlink；`import dollos.voice.asr_sherpa` 成功。
  - luxtts：monkeypatch `onnxruntime.get_available_providers` 只回 CPU 時，`preload_cudnn` 沒被呼叫；有 CUDA 時呼叫一次；log 有 provider。
  - 全套 `uv run pytest -q`（不加 `--ignore`）跑得完：0 failed，test_mic、test_speaker 和 qwen3 那 3 個是 skip 並寫原因。
- 解鎖：清單 1–8（Mac 拿得到 code 之後，見 Q2）。其他所有 task 都要它。

### Task 2 — M2 平台層，Mac 上 pulse 關掉（spec §2、§3.3）

- 檔案：新增 `src/dollos/host.py`；修改 `src/dollos/__main__.py`、`src/dollos/kernel.py`、`src/dollos/perception/system_pulse.py`（`read_loadavg`）、`src/dollos/prompts/templates/scaffolding.jinja`、`config.example.toml`、`pyproject.toml`（marker 登記）、`tests/conftest.py`、`tests/test_system_pulse.py`、建 `DollOS` 的測試和 scripts（見下）。
- 改法：
  - `host.py`：`Host = Literal["linux", "darwin"]`；`current_host()` 是 repo 裡唯一讀 `sys.platform` 的地方，其他值丟 `UnsupportedHostError`；`Absent(reason)`；`check_host(settings, host)`，這一步只有一條規則：darwin 上 `[system_pulse] enabled = true` 就拒絕，訊息 `system_pulse has no macOS readers yet; set [system_pulse] enabled = false`。
  - `__main__`：載入設定之後呼叫 `current_host()`（不支援就印 `unsupported platform: <sys.platform>`，以非 0 結束），log `host platform: <host>`，跑 `check_host`（失敗就印原因、以非 0 結束，不建 `DollOS`），再把 `host` 傳給 `DollOS`。`load_settings` 不看平台。
  - `DollOS(settings, *, host)`：`host` 必填，沒有預設值。測試裡 85 個 `DollOS(settings)`（10 個檔）一律改成傳 `host="linux"`，跟 ctl 測試一樣，在 Mac 上跑也是同一條路徑。測試以外的三個呼叫點（`scripts/persona_stability_smoke.py:187`、`experiments/lang-exp/capture.py:79`、`experiments/lang-exp/keeper.py:126`）傳 `host=current_host()`，不然它們一跑就 TypeError。
  - kernel：PulseObserver 只在 `enabled and alerts_enabled` 時啟動。啟動時 log 一次訊號清單和熱乘數原因，例如 `pulse signals: all absent (system_pulse disabled on darwin)`、`energy thermal multiplier: absent (no GPU temperature on darwin) -> 1.0`。Linux 開著 pulse 時行為不變。
  - scaffolding：`[Self pulse]` 那行（`:114`）和 Body 段（`:117-119`）包在 `{% if pulse_enabled %}` 裡；`split_scaffolding` 多傳 `pulse_enabled=settings.system_pulse.enabled`。
  - `read_loadavg` 改用 `os.getloadavg()` 和 `os.cpu_count()`。
  - `config.example.toml` 的 `[system_pulse]` 加一行註解：macOS 要設 `enabled = false`。預設值不改。
  - `conftest.py` 加 `linux_only`、`darwin_only`：依 `current_host()` 自動 skip，並寫出原因。`test_system_pulse.py::test_poll_now_populates_sample` 標 `linux_only`。
- 測試（Linux 上，monkeypatch `sys.platform` 或直接傳 `host`）：
  - `current_host()`：linux、darwin、win32 三種結果。
  - `check_host`：darwin 加 `enabled = true` 時拒絕，訊息照上面；darwin 加 false 通過；linux 用預設設定通過。monkeypatch 成 darwin 時，`load_settings` 照樣載得了預設設定。
  - `__main__`：不支援的平台以非 0 結束；`check_host` 失敗時沒有建 `DollOS`。
  - kernel：`enabled = false` 時沒有 `pulse-observer` task；host 是 darwin 時啟動 log 有那兩行。
  - scaffolding：`pulse_enabled=True` 渲染出來的跟今天一個 byte 都不差（Linux 的 cache 前綴不變）；False 時沒有 `Your body IS this computer`，也沒有 `[Self pulse]`。
  - grep 測試：`src/` 裡只有 `host.py` 出現 `sys.platform`。
- 解鎖：Task 4–6 都用到 `host.py`。清單 10 的「`enabled` 沒改時拒絕」那一半；清單 11 的 `host platform: darwin` 和 absent 清單。

### Task 3 — M3 Supervisor：daemon 死了，子行程也走（spec §3.1）

- 檔案：`src/dollos/service_supervisor.py`；新增 `src/dollos/_lifeline.py`；修改 `src/dollos/discord_bridge/__main__.py`、`src/dollos/mcp_server/__main__.py`、`src/dollos/kernel.py`（兩個 argv）、`CLAUDE.md`（`:72`）、`config.example.toml`（`:24`）、`docs/dollosctl-smoke.md`（`:236-255`，`kill -9` 那一項講 PDEATHSIG）、`tests/test_service_supervisor.py`；新增 `tests/test_lifeline.py`。
- 改法：
  - 刪掉 `_PR_SET_PDEATHSIG`、`_prctl_pdeathsig`、`_make_preexec`。spawn 改成 `create_subprocess_exec(*argv, stdin=PIPE, stdout=None, stderr=None, start_new_session=True)`；`_signal_group` 不變。
  - daemon 握著 `proc.stdin`，永遠不寫；子行程 reap 之後才關（`_SpawnAborted` 那條路也一樣）。
  - kernel 的 bridge argv（`kernel.py:640-646`）和 MCP argv（`:673-678`）加 `--exit-on-stdin-eof`。
  - `_lifeline.start_stdin_eof_watcher()`：開一條 daemon thread 讀 `sys.stdin.buffer` 到 EOF，然後 `os.kill(os.getpid(), SIGINT)`；5 秒（同 `_GRACE_S`）後還沒結束就 `os._exit(1)`。
  - bridge 和 MCP 的 parser 加 `--exit-on-stdin-eof`；有這個旗標時，`main` 在 `asyncio.run` 之前啟動 watcher。你在終端機手動跑時沒有這個旗標，不受影響。
  - CLAUDE.md、`config.example.toml`、`docs/dollosctl-smoke.md`、supervisor 模組說明裡講 PDEATHSIG 的地方，改成 stdin 管線。
- 測試：
  - 刪掉 `test_pdeathsig_kills_child_when_parent_dies`（`:269`），換成 `test_child_exits_on_parent_sigkill`：一個輔助 parent 跑 supervisor 和一個會啟動 watcher 的子行程；SIGKILL parent 之後，子行程 2 秒內消失。不加 marker，兩個 OS 都跑。
  - `_lifeline`：子行程裡啟動 watcher，關掉它的 stdin → 它收到 SIGINT、正常結束；一個忽略 SIGINT 的子行程約 5 秒後以 1 結束。
  - supervisor：spawn 的參數有 `stdin=PIPE`、`start_new_session=True`，沒有 `preexec_fn`；stdin 在 reap 之後才關。
  - bridge、MCP：有旗標才啟動 watcher。kernel：兩個 `ServiceSpec` 的 argv 都有這個旗標。
- 解鎖：清單 12、13、14（加上 Task 5）。

### Task 4 — M4 耐久：pidfile、原子寫入、壞掉的自己（spec §3.5）

- 檔案：`src/dollos/wal/pidfile.py`；新增 `src/dollos/durable.py`；七個寫入點；`src/dollos/mind/mind_state.py`；`tests/test_pidfile.py`、`tests/test_mind_state_durability.py`；新增 `tests/test_durable.py`。
- 改法：
  - pidfile：開 `daemon.pid`，整個生命週期握著 `fcntl.flock(LOCK_EX | LOCK_NB)`。鎖被占 → 明確錯誤「另一個 daemon 在跑」。拿到鎖之後：內容不是空的（有 pid，或看不懂，跟今天的 `test_corrupt_pidfile_treated_as_dirty` 一樣）→ DIRTY，空的或檔案不在 → COLD；接著寫自己的 pid（只給人看）。乾淨收尾時清空內容再放鎖，**不刪檔**。刪掉 `_pid_alive`。
  - `durable_replace(path, data)`：寫 tmp → flush → 依 `current_host()` 查表：darwin 用 `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)`，linux 用 `os.fsync(fd)` → `os.replace`。任何一步失敗，目標檔不變，tmp 刪掉，例外照樣往外丟（呼叫端今天怎麼處理寫入失敗就怎麼處理，例如 `save_state` 回 False）。
  - 七個寫入點改用它：`mind/mind_state.py:231-233`、`tools.py:41-46`、`schedule.py:62-79`、`mind/name_aliases.py:113-117`、`mind/evolution.py:112-115` 和 `:365-367`、`wal/perception_log.py:103-106`。append（WAL、記憶 markdown）照 spec 不改。
  - `load_state`：`mind_state.json` 不在、但旁邊有 `mind_state.json.corrupt-*` 時，丟 `MindStateLoadError`，訊息叫你把它還原或刪掉。
- 測試：
  - pidfile（真的 `flock`，兩個 OS 都跑）：另一個 process 握著鎖時 acquire 失敗；沒上鎖、內容有 pid → DIRTY；乾淨收尾之後 → COLD；收尾後檔案還在、內容是空的。現有的 `test_another_running_daemon_raises` 改成用握鎖的方式，不再用 `os.kill`；`test_clean_release_then_restart_is_cold` 斷言檔案被刪，改成斷言檔案在、內容空。
  - `durable_replace`：monkeypatch `current_host`、`fcntl.fcntl`、`os.fsync`；darwin 呼叫 `F_FULLFSYNC`（用 `monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)` 補上），linux 呼叫 `os.fsync`；sync 失敗時目標檔不變、沒有留下 tmp。
  - 七個寫入點各一條：寫入走的是 `durable_replace`。
  - `mind_state`：有 `.corrupt-*`、沒有主檔 → 丟例外；兩個都沒有 → 照舊是全新的 MindState。
- 解鎖：清單 13（launchd 重啟不會被舊 pidfile 擋住）、19（pidfile 沒擋過啟動）。

### Task 5 — M5 dollosctl → launchd（spec §3.2）

- 檔案：新增 `src/dollos/ctl/launchd.py`、`src/dollos/ctl/launchctl.py`；修改 `src/dollos/ctl/cli.py`、`tests/conftest.py`、`tests/test_ctl_*.py`、`CLAUDE.md`（dollosctl 段）；新增 `tests/test_ctl_launchd.py`、`tests/test_ctl_launchctl.py`。
- 改法：
  - `launchd.py`：純函式，用 `plistlib` 產生 `~/Library/LaunchAgents/dollos.daemon.plist`，key 和值完全照 spec §3.2 的表（`KeepAlive {SuccessfulExit: false}`、`ThrottleInterval 10`、`ExitTimeOut 90`、`ProcessType Interactive`、`LimitLoadToSessionType Aqua`、log 寫到 `~/Library/Logs/DollOS/daemon.log` 的絕對路徑等）。`ProgramArguments` 的 python 用 `resolve_params` 給的 venv 路徑（`sys.executable`），**不解析 symlink**：`.venv/bin/python` 解析後是 uv 的基礎 python，沒有 venv 的套件，launchd 下會 ImportError。
  - `launchctl.py`：純 argv builder（`bootstrap gui/<uid> <plist>`、`bootout gui/<uid>/dollos.daemon`、`print gui/<uid>/dollos.daemon`）加一個薄的 `_run`，錯誤是 `LaunchctlError`。`logs -f` 的 `tail -F` 走同一個模組裡薄的 `_exec`（今天 systemd 的 `journal` 是直接 `os.execvp`）。
  - `cli.main(argv=None, *, host=None)`：console script 用 `current_host()`；後端用一張表選（linux → systemd，不變；darwin → launchd）。CLI 邊界接 `SystemctlError`、`LaunchctlError` 和 `UnsupportedHostError`（印 `unsupported platform: …`，不印 traceback）。
  - darwin 的每個指令照 spec §3.2 的表：
    - `install`：載入設定、跑 `check_host`、兩條拒絕（python、working_dir、config、data_root、config 指到的 bridge／mcp 設定檔，解析 symlink 後任何一個落在 `~/Desktop`、`~/Documents`、`~/Downloads`、`~/Library/Mobile Documents`、`/Volumes` 底下；`[llm].base_url` 是私有區網的 IP literal）、建 LaunchAgents 和 log 目錄、寫 plist、印三個路徑和三段提示（已載入時要 `restart` 才生效；背景項目通知和在系統設定關掉它等於停掉她；log 不輪替）。
    - `start`：已載入時回明確錯誤。`stop`：`bootout`。`restart`：`bootout` 再 `bootstrap`。
    - `status`：`launchctl print` 原樣印出，只用 exit code 判斷「沒載入」；另外印 log 檔路徑和大小。
    - `logs`：`tail -n N [-F]` log 檔，並提示 `log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 10m`。
    - `uninstall`：`bootout`（沒載入不算錯）+ 刪 plist。
  - `install` 在兩個 OS 都載入設定並跑 `check_host`；Linux 目前沒有規則，輸出不變。這是 Linux 上唯一的行為改變：今天 `install` 不讀 config，之後 config 載不了就明確失敗。`--unit-dir` 在 darwin 指 plist 目錄。
  - `conftest.py` 加 autouse fixture：測試裡呼叫真的 `systemctl._run`、`launchctl._run` 或 `launchctl._exec` 一律丟例外。不加的話，你在 Mac 上跑 pytest 可能真的把她停掉。
  - 既有的 `tests/test_ctl_*.py` 一律明確傳 `host="linux"`。`test_ctl_install.py` 今天傳的是不存在的 `daemon.toml`（或只有 `[bridge]` 的片段），install 開始讀 config 之後會失敗：改成寫一份最小但合法的 config（`[llm]`、`[character]`），斷言不變。
  - darwin 的測試一律把 `HOME` 指到 `tmp_path`（`monkeypatch.setenv("HOME", …)`）：不然在 zenith 上跑會真的建出 `~/Library/LaunchAgents`、`~/Library/Logs/DollOS`，TCC 檢查也會看真的家目錄。
  - CLAUDE.md 的 dollosctl 段加 launchd 小節：plist 和 log 的位置、背景項目開關、log 不輪替。
- 測試：
  - plist：golden 測試，`plistlib.loads` 來回後每個 key 和值都對。
  - argv builder；`cli.main(..., host="darwin")` 加 mock runner 跑每個指令。
  - install：每個 TCC 位置都拒絕（含經過 symlink 的情況）；10/8、172.16/12、192.168/16、169.254/16 拒絕；100.x 和 hostname 通過，訊息說 hostname 不檢查；建了 log 目錄；`check_host` 失敗時什麼都沒寫。
  - Linux：既有測試的輸出不變。
- 解鎖：清單 10–16 裡 launchd 的部分（install、start、status、logs、stop、KeepAlive 重啟）。

### Task 6 — M6 睡眠（spec §3.8）

- 檔案：新增 `src/dollos/host_clock.py`；修改 `src/dollos/kernel.py`（`_diary_scheduler`、`_schedule_runner`、啟動 HostClock）、`src/dollos/mind/mind_prompt.py`（Awoke 那行）；新增 `tests/test_host_clock.py`、`tests/test_diary_window.py`。
- 改法：
  - 「主機累計睡了多久」依平台查表：darwin = `CLOCK_MONOTONIC − CLOCK_UPTIME_RAW`，linux = `CLOCK_BOOTTIME − CLOCK_MONOTONIC`。
  - `HostClock`：每 30 秒讀一次；比上次多了超過 5 秒就是主機睡過，多出來的就是 `offline_s`。送出 `Awoke{reason: "woke", offline_s}`，在 `data/telemetry/host_events.jsonl` 寫 `slept`／`woke`。不比牆上時間。下面的 `diary_sent`、`diary_missed`、`schedule_missed` 也都寫在這個檔。
  - 日記：每 30 秒看一次牆上時間；在 `[目標, 目標 + 10 分]` 之內、當天還沒送過才送 DiaryMoment，並記 `diary_sent{date}`。醒來時已經過了窗 → 不補寫，記 `diary_missed{date, reason: "host_asleep"}`。daemon 啟動時，最近一個已過的窗如果既沒送過也沒記過 → 記 `diary_missed{date, reason: "daemon_down"}`。
  - 排程：醒來時，睡著那段時間內到期、還沒送過的項目記 `schedule_missed`，不補跑。
  - Awoke 的 `woke` 那行寫出睡了多久（例如「the computer was asleep for 2h 10m」），她才知道離線多久。
- 測試（注入假的「睡了多久」計數和假的牆上時間，Linux 上跑）：
  - 睡過 23:00 → `diary_missed host_asleep`，沒有 DiaryMoment；睡著、在窗內醒來 → 送一次；沒睡 → 送一次。
  - event loop 卡住 120 秒（牆上時間跳了、計數沒動）→ 不算睡。
  - daemon 在窗內重啟 → 不送第二次；daemon 整晚沒跑 → `daemon_down`。
  - 睡著時到期的排程 → `schedule_missed`，沒有 ScheduledMoment。
  - darwin 的計數函式也在 Linux 上測：`monkeypatch.setattr(time, "CLOCK_UPTIME_RAW", <常數>, raising=False)` 加假的 `time.clock_gettime`（跟 `F_FULLFSYNC` 同一招），確認讀的是 `CLOCK_MONOTONIC − CLOCK_UPTIME_RAW`；`host="linux"` 時完全不碰 `CLOCK_UPTIME_RAW`。真的時鐘在 Mac 清單第 17 步驗。
- **Task 6b（只在 Q1 = 要的時候做）**：`[host] keep_awake: bool = false`；`check_host` 加一條：Linux 上設了就拒絕；darwin 上開著時，daemon 啟動 spawn `/usr/bin/caffeinate -s -w <daemon pid>` 並 log 一行。測試：mock spawn，argv 正確；Linux 被拒絕。
- 解鎖：清單 17；6b 解鎖 18。

### Task 7 — M7 語音 bridge（spec §3.4）

- 檔案：`src/dollos/voice/bridge/__main__.py`、`mic.py`、`speaker.py`；新增 `src/dollos/voice/bridge/devices.py`（不 import sounddevice，zenith 上測得到）、`tests/voice/bridge/test_devices.py`、`tests/voice/bridge/test_bridge_wiring.py`。
- 改法：
  - `--mic-device` 和新的 `--speaker-device` 接受編號或名稱片段（`"3"` → 3，`"MacBook"` → 字串原樣交給 sounddevice）；`SpeakerPlayer` 收 `device`。
  - 麥克風開始後檢查前 2 秒：全是 0 就以 exit 2 結束，訊息照 spec §3.4（麥克風被拒或靜音，以及系統設定的路徑）。
- 測試：
  - `devices.py`：裝置參數的解析；靜音判斷（2 秒全 0 → 靜音；任何一個非 0 → 正常；還不到 2 秒 → 還不判斷）。
  - 接線（`__main__` 在靜音時回 2、mic 和 speaker 收到 device）寫在新的 `tests/voice/bridge/test_bridge_wiring.py`：用 `monkeypatch.setitem(sys.modules, "sounddevice", <假模組>)` 換掉 sounddevice（並把已載入的 `dollos.voice.bridge.mic`／`speaker`／`__main__` 從 `sys.modules` 拿掉再 import），所以 zenith 上也跑得到。
- 解鎖：清單 15。

### Task 8 — R1a 前置：取消註冊 Shell、SpawnWorkflow、SpawnMonitor

spec §5 把它列為 M8 的前置條件、不在 spec 裡實作；這個決定你已經核可，所以放進這份 plan，排在 M8 之前。

- 檔案：`src/dollos/tools.py`（`MAIN_TOOLS`，`:1280-1287`）、`src/dollos/prompts/templates/scaffolding.jinja`、`character_packs/yesman/doll.toml`（`:48-49`）、`character_packs/powdur/doll.toml`（`:62`）、`CLAUDE.md`（Key Architecture Decisions 講 fire-and-forget 的那一條）、受影響的測試。
- 改法：
  - `MAIN_TOOLS` 拿掉 `Shell`、`SpawnWorkflow`、`SpawnMonitor`，以及 `RemoveMonitor`（它只能關 SpawnMonitor 開的 monitor）。`REFLECTION_TOOLS` 跟著變。工具類別、runner、`SUB_TOOLS` 都留著，只是沒有路徑叫得到（`SUB_TOOLS` 只有 SpawnWorkflow 的 worker 用）。
  - `ReadToolOutput`、`GrepToolOutput` 留在 `MAIN_TOOLS`（redesign 保留 `read_output`），雖然之後沒有東西產生 `output_id`；ReadToolOutput 說明裡的「Tool outputs come from Shell / Subagent」這次不改，R2 重寫。
  - scaffolding：刪掉教她用這些工具的段落：`:21-23`、`:25-27`、`:24` 裡「MANDATORY: before calling Shell or SpawnWorkflow…」那句（Scratchpad 本身的說明留著）、`:74` 列舉裡的 `Shell`、`SpawnWorkflow`、`:135`「Writing a new skill → use Shell」。
  - 角色包：yesman（`config.example.toml` 的預設包）和 powdur 的 taboos 寫著「if I need to run Shell, actually call the Shell tool」，會被渲染進 system prompt。改成不點名工具（「if I need a tool, I actually call it」）。gura 沒有這句。
  - 部署後，internal 的工具清單和 system 變了一次，所以會開一次新的一頁。這是預期的。
- 測試：
  - internal 給模型的工具 JSON 裡，工具名稱（`function.name`）沒有那 4 個；SpawnMonitor 說明裡的 `nvidia-smi`、`/var/log/syslog` 不會再出現在 prompt 裡。（不要整串比對 `"Shell"`：ReadToolOutput 的說明還有這個字。）
  - 用 yesman、powdur、gura 三個包各渲染一次 scaffolding：都沒有 `Shell`、`SpawnWorkflow`、`SpawnMonitor`、`RemoveMonitor`。
  - 在 scratch 模擬過只改 `MAIN_TOOLS` 和 scaffolding：**10 個檔、17 個既有測試會壞**。`test_external_safety.py`（1）、`test_external_tool_gate.py`（2）、`test_mind_loop_agenda_registry.py`（3）、`test_mind_loop_pulse.py`（1）是權限邊界的測試：改用測試自己的 registry（含 `Shell`，類別還在），**斷言照留**，不能因為 Shell 不在 `MAIN_TOOLS` 就刪掉。`test_mind_loop.py`（1）、`test_mind_loop_model.py`（4）、`test_mind_loop_trace.py`（1）、`test_mind_loop_action_log.py`（1）測 fire-and-forget 和工具 JSON：同樣改用自己的 registry。`test_tools.py` 的 `test_shell_in_tools_list`、`test_main_tools_includes_spawn_workflow_not_report` 反過來斷言「不在」。`test_prompt_renderer.py::test_scaffolding_documents_tool_output_paging` 刪掉（那段說明跟著 `:23` 刪了）。
- 解鎖：M8 的前置條件（清單第 10 步之前要在 main 上）。

### Task 9 — 文件收尾

- 檔案：`docs/superpowers/specs/2026-10-18-dollos-redesign-design.md`、`CLAUDE.md`。
- 改法：照 spec §8 的表改 redesign spec 的六處（sherpa-onnx-core、不裝 Homebrew 的 portaudio、LaunchAgent 的 key 和兩條拒絕、HostClock 提供醒來的 Awoke、「macOS 26 以上」、Energy 列的行號）；CLAUDE.md「下一個」第 4 項的「尚無 spec」改成指向 spec 和這份 plan。
- 測試：無，只改文件。
- 做完之後：用 `superpowers:finishing-a-development-branch` 合進 main；main checkout 的 `.venv` 照 Task 1 刪掉 symlink 再 `uv sync`。push 照 Q2，先問你。

### Task 10 — M8 Mac 驗收（在 Mac 上，由 Mac 上的 Claude Code 做）

- 交給它的東西：spec §4.2 的清單（19 步，照順序，每一步都要過）和這一段。
- 前置條件（spec §5），清單第 10 步之前都要做好：
  - Task 8 已經在 main 上。
  - 別的機器上沒有在跑的她（zenith 上 `pgrep -fl 'dollos'` 沒有 daemon；有用 dollosctl 裝的話先 `uv run dollosctl stop`。2026-10-21 查過：zenith 沒有裝 unit，也沒有在跑）：同一個 Discord bot token 只能有一個 bridge，她也只能有一份記憶。
  - Mac 的 `data/` 是照 redesign R1a「搬記憶」搬過去的那份。搬之前在 zenith 上確認 `find data | tr A-Z a-z | sort | uniq -d` 沒有輸出（APFS 預設不分大小寫）。
- 某一步沒過：Mac 上的 Claude Code 只記錄、不改 code；回 zenith 修好、合進 main 之後，再從那一步重跑。
- 第 14 步如果發現 `bootout` 不等 process 結束，回 zenith 加一個小修正：`restart` 先等 pidfile 的鎖放掉再 `bootstrap`（spec §6）。不預先做。
- 結果寫成一頁 `docs/research/2026-10-XX-macos-port-mac-results.md`：每一步過或不過、luxtts 在 launchd 下的 RTF、72 小時的 log 成長量、記憶體用量（活動監視器）、有沒有跳區域網路的詢問、aiortc 選中的 candidate pair。筆記怎麼回到 zenith，跟 Q2 選的路一樣。

---

## 驗收

### zenith（Linux）上

- 全套 `uv run pytest -q`（不加 `--ignore`）：0 failed；每個 skip 都有寫原因。跟基準比，不見的測試只有刻意刪掉的：PDEATHSIG 那一個（Task 3）和 `test_scaffolding_documents_tool_output_paging`（Task 8）。
- `grep -rn "sys.platform" src/` 只有 `host.py`；`grep -rn "prctl\|preexec_fn\|_ensure_sherpa_onnx_symlink" src/` 沒有結果。
- `uv lock --check` 通過；跟 Task 0 比，lock 裡的版本沒變，只多了 `sherpa-onnx-core` 和 marker。
- 手動跑一次（scratch 設定：`[data] root` 和 `[trace] root` 指到新的空目錄，IPC 換一個沒人用的 port，`[mcp]` 開、`[bridge]` 關，所以不碰 zenith 上的她）：
  - `uv run python -m dollos --config <scratch>.toml` 起得來，log 有 `host platform: linux` 和兩個引擎的 provider。
  - `pgrep -fl dollos` 看得到 `dollos.mcp_server`；`kill -9` daemon 之後，MCP 子行程 2 秒內消失。
  - 再起一次：沒有被 pidfile 擋住，Awoke 是 `recovered`；`Ctrl-C` 收尾之後 `daemon.pid` 還在、內容是空的。
- `uv run dollosctl status`、`uv run dollosctl logs daemon -n 20` 的輸出格式跟今天一樣（只讀；zenith 今天沒有裝 unit，所以 `status` 印空白、`logs` 印 `-- No entries --`，兩個都回 0，前後一樣就算過）。

### Mac 上的清單（交給 Mac 上的 Claude Code）

清單本身在 spec §4.2，這裡不重抄。每一步要哪些 task 先合進 main：

| 清單步驟 | 需要的 task |
|---|---|
| 1–8（安裝、import、ORT、沒有 torch、FTS5、PortAudio、連得到 Strata） | Task 1，加上 Mac 拿得到 code（Q2） |
| 9（Mac 上全套 pytest） | Task 1–9 全部 |
| 10（install、start；`enabled` 沒改時拒絕） | Task 2、5，加上 Task 10 的前置條件 |
| 11（status、log 內容） | Task 1、2、5 |
| 12（bridge 和 MCP 起得來、60 秒沒有 Down） | Task 3、5 |
| 13（`kill -9` 之後子行程消失、launchd 重啟） | Task 3、4、5 |
| 14（`stop` 之後全部乾淨結束） | Task 3、5 |
| 15（語音一輪對話） | Task 1、5、7 |
| 16（RTF ≤ 0.5） | Task 1、5 |
| 17（`pmset sleepnow`，醒來的 Awoke 和 `offline_s`） | Task 6 |
| 18（caffeinate 的 assertion） | Task 6b，只在 Q1 = 要 |
| 19（無人看管 72 小時） | 全部，加上 Task 10 的前置條件 |

---

## 不在範圍內

- spec §1 的「不做」：UI、她的 VM、Discord bridge 和 MCP 的功能、Mac 的 pulse 讀取器（以及 Linux 讀取器的 `None` → `Absent`，兩邊同一步做，在 v1 之後）、`mem_pressure`、log 輪替、sudo、root 的 LaunchDaemon、Full Disk Access、FileVault 重開機後免登入啟動。
- `uv lock --upgrade` 和 sherpa 的升級；`docs/roadmap.md:410` 那句過時的 onnxruntime 上限。
- redesign R1a 的其他項目：IPC token 和 Origin、`dollos chat`、搬記憶本身、Discord `dm_only`、刪掉每日 bootstrap 的 ScheduledMoment。
- 刪掉 Shell、Workflow、Monitor 的類別和 runner（這次只取消註冊）；之後要看顧別人的程式時的反孤兒設計。
- `restart` 等 pidfile 的鎖（第 14 步證明需要才做）；`[host] keep_awake`（Q1 = 要才做，Task 6b）。
