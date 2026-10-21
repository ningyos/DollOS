# macOS 移植：Doll 的 daemon 搬到你的 Mac

**狀態**：APPROVED（使用者 2026-10-21「LGTM」；同時核可它依賴的兩個 redesign R1a 決定：Mac 上關掉 system pulse、每天使用前取消註冊 Shell／SpawnWorkflow／SpawnMonitor）
**日期**：2026-10-21
**關聯**：`2026-10-18-dollos-redesign-design.md`（DRAFT；本文是它 R1a「Mac 移植」那一項的細部設計，修正列在 §8）、`2026-07-06-bridge-internalization-design.md`（ServiceSupervisor）、`2026-07-09-system-pulse-proactive-trigger-design.md`、`2026-07-09-system-pulse-presence-tuning-design.md`、`2026-07-10-metabolic-vital-model-design.md`（熱乘數）、`plans/2026-10-18-gura-english-luxtts-mac.md`、CLAUDE.md 的「dollosctl」段
**目標機器**：Apple M5、16 GB、macOS 27.0.1；沒有 UI；大模型在 Strata（`http://100.110.158.36:8080`，走 Tailscale）

---

## §0 白話摘要

1. Doll 的 daemon 改成在你的 Mac 上由 launchd 看顧（Linux 開發機照舊用 systemd）；Discord bridge 和 MCP 這兩個子行程改用一條 stdin 管線知道「daemon 死了」，兩個系統用同一套做法。
2. 補上一個漏掉的套件（`sherpa-onnx-core`），不然 Mac 上 daemon 一 import 就掛；她的檔案改成斷電也不會變空白。
3. 程式啟動時依平台明確選 Linux 或 macOS 的實作；Mac 給不出的訊號一律標成「沒有，原因是…」，不假裝、不拿別的東西代替。
4. Mac 上 system pulse 關掉（redesign 已決定）：沒有 GPU 熱、電池、閒置的身體感，energy 的熱乘數固定 1.0，啟動 log 寫明原因。
5. Mac 睡著她也睡；醒來時她收到「睡了多久」，睡著時錯過的日記和提醒會被記錄下來，不補跑。
6. 你在 Mac 上要做一次：repo 放在 `~/DollOS/DollOS`（不要放桌面、文件、下載、iCloud、外接碟），裝 Xcode Command Line Tools 和 uv，Tailscale 用 Standalone 版並關掉兩台機器的 key 到期，第一次開語音時允許終端機用麥克風（python 可能另外問一次「區域網路」，§3.7）。main 要先 push 上 GitHub，Mac 才拿得到（push 前會先問你）。Mac 上的她正式跑起來前，zenith 上的她要先停：她只能有一個。
7. 不做：UI、她的 VM、Discord 的改動、Mac 上的身體感讀取器（v1 之後）、log 輪替、FileVault 重開機後免登入啟動。

---

## §1 範圍與不做的事

**範圍**：讓今天 main 上的 daemon（文字、語音、Discord bridge、MCP server）在 Mac 上用 `uv sync` 裝起來、由 `dollosctl` 交給 launchd 長期跑，行為跟 zenith 上一樣；做不到一樣的地方明確寫出來。

**不做**：
- 不做 UI（桌寵之後另寫 spec）。
- 不做她的電腦（VMPal、`computer` 工具，redesign R1b/R4）。
- 不改 Discord bridge 和 MCP server 的功能，只改它們的進入點（§3.1 的 EOF watcher）。
- 不砍 redesign 以外的功能；redesign 要砍或延後的（pulse、Shell 等）照它的步驟做，本文不重複。
- 不做 Mac 的 pulse 讀取器（§3.3 列出來源，實作在 v1 之後）。
- 不做 log 輪替、不用 sudo、不要求 Full Disk Access、不跑 root 的 LaunchDaemon。

---

## §2 平台層

新模組 `src/dollos/host.py`，很小：

```python
Host = Literal["linux", "darwin"]

def current_host() -> Host: ...          # 只在這裡讀 sys.platform；其他值 → UnsupportedHostError

@dataclass(frozen=True)
class Absent:
    reason: str                          # 例："macOS: no unprivileged GPU temperature"
```

規則：
1. **只有 `host.py` 讀 `sys.platform`**。daemon 和 `dollosctl` 在啟動時呼叫 `current_host()`，log 一行 `host platform: darwin`，再把 `host` 當參數傳給需要的元件（ctl 後端、HostClock、keep_awake）。散在七個呼叫點的 `durable_replace`（§3.5）不收參數，自己呼叫 `current_host()`；測試用 monkeypatch 換掉它。不支援的平台直接結束，訊息寫 `unsupported platform: <sys.platform>`。
2. **選實作用一張表**：`{"linux": A, "darwin": B}[host]`。不准用 `try: import … except ImportError`、`shutil.which(...) is None`、`FileNotFoundError` 來「猜」平台或換實作。
3. **給不出的訊號就是 `Absent(reason)`**，不是靜默的 `None`，也不拿別的來源頂替（例如不用 `vm_stat` 湊一個「記憶體使用率」）。
4. **呼叫端明確處理 `Absent`**：block 不渲染那一行，但啟動時 log 一次完整清單；警示不會因它觸發；乘數用 1.0 並 log 原因（§3.3）。
5. **需要 absent 訊號的設定在啟動時就拒絕**（例如 darwin 上 `[system_pulse] enabled = true`）：`check_host(settings, host)` 在 daemon 的 `__main__`（建 `DollOS` 之前）和 `dollosctl install` 裡跑，不是跑起來才發現。`load_settings` 本身不看平台：`enabled` 的預設是 true，如果在載入時檢查，Mac 上所有用預設設定的測試都會被拒絕。
6. 元件的函式收 `host` 參數，所以 Linux 上的測試可以直接測 darwin 的路徑（§4.1）。

哪些地方依平台分：`dollosctl` 的後端（systemd / launchd）、`durable_replace` 的同步方式（`os.fsync` / `F_FULLFSYNC`）、`keep_awake`（只有 darwin）、HostClock 讀的「睡了多久」時鐘（§3.8）、之後的 pulse 讀取器。哪些不分（兩邊同一份 code）：ServiceSupervisor、pidfile、HostClock 的其餘部分和日記排程、語音、`os.getloadavg()`。

---

## §3 各元件

### §3.1 ServiceSupervisor：daemon 死了，子行程也要走

**今天**：spawn 時在 `preexec_fn` 裡 `setsid` + `prctl(PR_SET_PDEATHSIG, SIGINT)` + `getppid` 檢查（`service_supervisor.py:32-56`、`:103-108`）。macOS 的 libc 沒有 `prctl`，`preexec_fn` 丟例外，CPython 變成 `SubprocessError`，被 `_supervise` 當成 crash（`:118-128`）；退避 1+2+4+8+16 秒後放棄，`on_gave_up` 送出 BridgeDown/McpDown（`kernel.py:651-667`、`:685-695`）。結果：Mac 上每次開機約 31 秒後她就「感覺到」Discord 斷了。另外 `preexec_fn` 在有執行緒的 process 裡 fork 後跑 Python，CPython 文件說不安全。

**設計**（兩個 OS 同一套，redesign R1a 已提）：
- 拿掉 `_prctl_pdeathsig`、`_make_preexec`。spawn 改成 `create_subprocess_exec(*argv, stdin=PIPE, stdout=None, stderr=None, start_new_session=True)`。`start_new_session` 在 C 裡做 `setsid`，執行緒安全；`_signal_group` 的 `killpg`/`getpgid`（`:80-84`）不變。
- daemon 握著 `proc.stdin`，永遠不寫，子行程 reap 之後才關。
- `kernel.py:640-646`（bridge）和 `:668-680`（MCP）的 argv 加 `--exit-on-stdin-eof`。只有 supervisor 會傳這個旗標；你在終端機手動跑 bridge 時不受影響。
- 新的 `dollos/_lifeline.py`：`start_stdin_eof_watcher()` 開一條 daemon thread，讀 `sys.stdin.buffer` 直到 EOF，然後 `os.kill(os.getpid(), SIGINT)`，也就是今天 PDEATHSIG 走的同一條優雅結束路徑（bridge `__main__.py:296-313` 的 `KeyboardInterrupt`）。送出後 5 秒（同 `_GRACE_S`）還沒結束就 `os._exit(1)`：daemon 已經死了，沒有人會像 `_stop_one` 那樣升級成 SIGKILL，卡住的舊 bridge 會跟新 daemon 起的 bridge 搶同一個 Discord token。`discord_bridge/__main__.py` 和 `mcp_server/__main__.py` 在旗標存在時呼叫它；兩者今天都不讀 stdin（MCP 走 streamable HTTP，不是 stdio）。
- 為什麼可靠：daemon 不管怎麼死（包括 SIGKILL），kernel 都會關掉它的 fd，子行程就讀到 EOF；如果 daemon 在子行程開始讀之前就死了，EOF 是立刻的，所以 `getppid` 的競態檢查可以拿掉。`os.pipe` 的 fd 預設不可繼承（PEP 446），`close_fds=True` 是預設，所以兄弟服務和孫行程都拿不到寫入端。
- macOS 上沒有 subreaper，孤兒會被 launchd（pid 1）收養；子行程有自己的 session，launchd 收 process group 時也不會收到它們。所以在 Mac 上 EOF watcher 是唯一的反孤兒機制，必須有兩個 OS 都跑的測試（§4.1）。
- 只適用於我們自己的 Python 子行程。Shell 和 SpawnMonitor 的子行程（`shell_runner.py:94`、`monitor_runner.py:176`）也是 `start_new_session=True`：daemon 被 SIGKILL 時，Linux 靠 systemd 收掉整個 cgroup（`KillMode=mixed`，`ctl/units.py:67`），Mac 上沒有人收，monitor 這種長命令會一直留著。本文不替它們做反孤兒：redesign R1a 在每天使用前就取消註冊 Shell、SpawnWorkflow、SpawnMonitor（SpawnMonitor 的說明裡還在教 `nvidia-smi` 和 `/var/log/syslog`，`tools.py:450-455`），這列為 M8 的前置條件（§5）。之後如果要看顧別人的程式，要另外設計。

### §3.2 dollosctl → launchd

**今天**：所有指令都呼叫 `systemctl --user`/`journalctl --user`（`ctl/systemctl.py:36-57`、`:73-130`）。Mac 上 `FileNotFoundError('systemctl')` 不是 `SystemctlError`，CLI 邊界（`ctl/cli.py:236`）接不到，你會看到 traceback。

**設計**：`ctl/cli.py` 用 `current_host()` 選後端：linux → systemd（不變），darwin → launchd。新增兩個檔案，結構照抄既有的：
- `ctl/launchd.py`：純函式，用 stdlib `plistlib` 產生 plist（對應 `ctl/units.py`）。
- `ctl/launchctl.py`：純 argv builder 加一個薄的 runner，錯誤是 `LaunchctlError`，CLI 邊界跟 `SystemctlError` 一起接（對應 `ctl/systemctl.py`）。

plist（`~/Library/LaunchAgents/dollos.daemon.plist`）：

| key | 值 | 對應 systemd（`ctl/units.py:47-69`） |
|---|---|---|
| `Label` | `dollos.daemon` | unit 名 |
| `ProgramArguments` | `[<venv 的 python 絕對路徑>, -m, dollos, --config, <config 絕對路徑>]` | `ExecStart` |
| `WorkingDirectory` | install 時的目前目錄（絕對路徑） | 同 |
| `EnvironmentVariables` | `PATH=/usr/bin:/bin:/usr/sbin:/sbin`、`PYTHONUNBUFFERED=1`、`PYTHONUTF8=1` | launchd 不跑 login shell、不展開 `~` |
| `RunAtLoad` | true | `WantedBy=default.target` |
| `KeepAlive` | `{SuccessfulExit: false}` | `Restart=on-failure`；SIGTERM 收尾後回 0（`__main__.py:27-31`），不會被重啟 |
| `ThrottleInterval` | 10 | `RestartSec=3`；10 是 launchd 的預設值，不調短（crash loop 時每次重啟都會寫一段錯誤）。跑超過 10 秒才死的，launchd 立刻重啟 |
| `ExitTimeOut` | 90 | systemd 預設 `TimeoutStopSec`；收尾要等進行中的回合（`kernel.py:1322-1325`） |
| `ProcessType` | `Interactive` | 不設會被輕度節流 CPU/IO；`Background` 只用 E-core。0.27 的 RTF 是在前景量的 |
| `LimitLoadToSessionType` | `Aqua` | 使用者登入的 GUI session |
| `StandardOutPath`、`StandardErrorPath` | `~/Library/Logs/DollOS/daemon.log`（寫成絕對路徑；目錄由 `install` 建，launchd 不會自己建，目錄不在 job 就起不來） | journald；子行程繼承 stderr，也寫在這裡 |

指令（domain 是 `gui/$(id -u)`，只在你登入 GUI 後存在）：

| 指令 | systemd（今天） | launchd |
|---|---|---|
| `install` | 寫 unit + `daemon-reload` + 清 legacy bridge unit | 跑 `check_host`（§2）；建 `~/Library/LaunchAgents` 和 log 目錄；寫 plist；不清 legacy（Mac 沒有）。plist 在 LaunchAgents 裡就等於「下次登入自動啟動」，相當於 systemd 的 enable。已載入時 launchd 不會重讀 plist，`install` 會印「跑 `restart` 才生效」 |
| `start` | `systemctl --user start` | `launchctl bootstrap gui/UID <plist>`；已載入時回明確錯誤 |
| `stop` | `systemctl --user stop` | `launchctl bootout gui/UID/dollos.daemon`（送 SIGTERM，等 ExitTimeOut）。plist 留著，下次登入她會回來，跟 systemd 停掉一個 enabled unit 一樣；要她下次登入也不回來，用 `uninstall` |
| `restart` | `systemctl --user restart` | `bootout` 再 `bootstrap`。不用 `kickstart -k`：它用什麼訊號殺沒有確認，可能跳過優雅收尾 |
| `status` | `systemctl --user status` | `launchctl print gui/UID/dollos.daemon` 原樣印出（man page 說它的格式「NOT API」，所以不解析）；只看 exit code 判斷「沒載入」。另外印 log 檔路徑和大小 |
| `logs [-f] [-n N]` | `journalctl --user -u` | `tail -n N [-F] ~/Library/Logs/DollOS/daemon.log`。launchd 自己的錯誤在 unified log：訊息裡提示 `log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 10m` |
| `uninstall` | stop + 刪 unit | `bootout`（沒載入不算錯）+ 刪 plist |

**從 repo 根目錄 install 的規則**照舊：`WorkingDirectory` 取目前目錄，`install` 印出 python、working_dir、data_root 的絕對路徑（`ctl/cli.py:118-126`）。`--unit-dir` 在 darwin 指 plist 目錄（測試用）。macOS 多兩條拒絕（明確錯誤，不是警告）：
1. python、working_dir、config、data_root，以及 config 指到的 bridge／mcp 設定檔，解析 symlink 之後任何一個在 `~/Desktop`、`~/Documents`、`~/Downloads`、`~/Library/Mobile Documents` 或 `/Volumes` 底下就拒絕：這些是 TCC 保護的位置，LaunchAgent 裡的 python 會被擋（EPERM，或跳一個沒人看得到的詢問），從終端機跑卻正常，很誤導。建議放 `~/DollOS/DollOS`。
2. config 的 `[llm].base_url` 如果是私有區網的 IP literal（10/8、172.16/12、192.168/16、169.254/16）就拒絕，原因見 §3.7。這是保守的近似：macOS 判斷「區網」看的是介面，不是 IP 範圍。hostname（例如 MagicDNS）不檢查，訊息會說這點。

**其他事實**（寫進 install 的輸出）：加 plist 時 macOS 會跳「已加入背景項目」通知；在「系統設定 › 一般 › 登入項目與延伸功能」把它關掉，就等於停掉她，`dollosctl` 不會知道。launchd 不輪替 log；`status` 會印大小，72 小時試跑時量成長速度再決定。

### §3.3 system_pulse 在 macOS

**今天**：每個讀取器在 Linux/X11 來源不存在時回 `None` 或 `[]`，模組說明寫「silently omitted」（`perception/system_pulse.py:1-14`）。Mac 上除了網路探測全部是 `None`，而 `enabled` 預設是 true（`config.py:149`、`config.example.toml:179-180`），`[Self pulse]` 只剩一行、警示永遠不會觸發、熱乘數停在 1.0，而且沒有任何訊息說明。這正是專案規則禁止的靜默降級。

**v1 決定**（照 redesign §3「System pulse… defer」）：Mac 的 config 是 `[system_pulse] enabled = false`。
- 啟動時的 `check_host`（§2 規則 5）在 darwin 上看到 `enabled = true` 就拒絕：`system_pulse has no macOS readers yet; set [system_pulse] enabled = false`。`config.example.toml` 的 `[system_pulse]` 加一行註解說 macOS 要設 false；預設值不改，Linux 照舊。
- `enabled = false` 時不啟動 PulseObserver（今天只看 `alerts_enabled`，`kernel.py:1216-1220`）。
- scaffolding 的 Body 段和 `[Self pulse]` 那行（`prompts/templates/scaffolding.jinja:114`、`:117-119`，「Your body IS this computer… GPU heat is your body heat」）只在 pulse 開著時渲染。redesign R1a 會整段拿掉；在那之前這樣做，Mac 上的她就不會讀到不實的身體描述。
- 啟動時 log 一次訊號清單，例如 `pulse signals: all absent (system_pulse disabled on darwin)`、`energy thermal multiplier: absent (no GPU temperature on darwin) -> 1.0`，這樣 1.0 不會被讀成「量到很涼」。

八個訊號在 macOS 上的來源（實作在 v1 之後；Linux 那邊同一步把 `None` 改成 `Absent(reason)`）：

| 訊號 | Linux 來源 | macOS 來源 | 權限 | 用到它的地方 |
|---|---|---|---|---|
| load1 + ncpu（thought load） | `/proc/loadavg`（`:124-130`） | `os.getloadavg()`，兩邊共用。M5 的 P+E 核心一起算，0.4/1.0 的 bucket 要在 Mac 上重看 | 無 | `[Self pulse]`、signature |
| mem_used_pct（memory） | `/proc/meminfo`（`:133-148`） | 不做百分比（macOS 沒有 MemAvailable 的對應）。另一個欄位 `mem_pressure`：`sysctl -n kern.memorystatus_vm_pressure_level`（1/2/4 → comfortable/tight/pressured，值要在 Mac 上確認） | 無 | `[Self pulse]`、signature |
| gpus（mem%、°C；vital heat） | nvidia-smi（`:179-221`） | **Absent**("macOS: no unprivileged GPU temperature")：要 root（powermetrics）或私有 API；統一記憶體也沒有 GPU 記憶體。她的思考在 Strata 的 GPU 上，Mac 的溫度本來就量不到她的費力 | — | gpu_hot 警示（`mind/pulse_observer.py:91-98`）→ `[Body signal]`（`mind/mind_prompt.py:679-706`）；熱乘數（`mind/mind_loop.py:948-970`）；VitalsRecord |
| gpu_power | nvidia-smi | **Absent**("macOS: GPU power needs root")；LaunchAgent 不准跑 sudo | root | 熱度那行的後綴、VitalsRecord |
| battery_pct + status（charge） | `/sys/class/power_supply`（`:226-241`） | `pmset -g batt`，以 `InternalBattery` 那行和 `%` 為錨；桌機是 **Absent**("no internal battery")。狀態明確對照：discharging、charging、charged/finishing charge → full、`AC attached; not charging` 自成一類；不認得的狀態 log 成錯誤，不猜 | 無 | battery_critical 警示（`mind/pulse_observer.py:82-89`，比對字串 `discharging`）→ `[Body signal]` |
| active_window（attention） | xdotool（`:244-253`） | **Absent**("not collected on darwin by design")：redesign §5 不感測你的視窗。標題要螢幕錄製或輔助使用權限 | — | window_stuck 警示永遠不觸發 |
| idle_s（breath） | xprintidle（`:256-265`） | `ioreg -c IOHIDSystem` 的 `HIDIdleTime`（奈秒） | 無 | `[Self pulse]`、window_stuck 的在場閘門、consolidation 的否決（R2 刪） |
| network_open（outside link） | TCP 1.1.1.1:53（`:268-281`） | 同一份 code | 無 | `[Self pulse]`、signature；醒來後短暫顯示 silent |

**對下游的影響**（v1 的 Mac）：沒有 PulseMoment，所以不會有 `[Body signal]`；energy 照 metabolic spec 的 absent 規則，乘數 1.0、ambient 欄位 `None`（`mind/mind_loop.py:952-970`，redesign §3 的 Energy 列）。之後若想要 Mac 的熱感，用公開的 thermal pressure（`notifyutil -g com.apple.system.thermalpressurelevel`）做一個另外命名的訊號和自己的乘數對照，不偽裝成攝氏溫度。

### §3.4 語音

- **sherpa-onnx（阻擋項）**：Mac 的 `_sherpa_onnx.cpython-313-darwin.so` 要 `@rpath/libonnxruntime.1.24.4.dylib`，這個檔只在另一個套件 `sherpa-onnx-core` 裡。uv 的 universal lock 讀的是 sdist 的空 metadata，所以 `uv.lock` 從來沒有 core；zenith 能動是因為 `asr_sherpa.py:19-57` 在 import 時往 site-packages 塞一個 symlink，而它只找 `libonnxruntime.so.*`，Mac 上什麼都不做。kernel 透過 `voice/__init__.py:8` 一定會 import 它（`kernel.py:63`），所以 Mac 上 `python -m dollos` 直接 ImportError。修法：pyproject 釘 `sherpa-onnx==1.13.1` 和 `sherpa-onnx-core==1.13.1`（不釘版本會解出 core 1.13.8，對不上），刪掉 `_ensure_sherpa_onnx_symlink`（core 在 Linux 也帶 `libonnxruntime.so`）。之後升 sherpa 時兩個一起升。
- **CUDA 預載**：`preload_cudnn()` 在 `asr_sherpa.py:60-62` 和 `tts_luxtts.py:21` import 時就跑。ASR 只用 CPU，拿掉；luxtts 改成解析出的 provider 是 CUDA 時才呼叫。兩個引擎都在 INFO log 實際的 ORT providers。
- **luxtts**：鎖在 luxtts-onnx main `d29f673`，`device = "auto"` 在 Mac 上就是 CPU（`luxtts_onnx/inference.py:217-227`），這是你 10-18 的決定，照舊。如果之後改釘 `feat/apple-silicon`，要重看 auto 的意思。0.27 的 RTF 是用 ORT 1.31 在前景量的；DollOS 鎖的是 1.24.4，所以 M8 在 launchd 下重量一次。
- **PortAudio**：sounddevice 0.5.5 的 macOS wheel 自帶 `libportaudio.dylib`，但它先用 `ctypes.util.find_library('portaudio')` 找系統的，找到就用那份（`sounddevice.py:62-91`）。Apple Silicon 的 Homebrew 在 `/opt/homebrew/lib`，不在 `find_library` 的預設搜尋路徑（`~/lib`、`/usr/local/lib`、`/lib`、`/usr/lib`）裡，所以 `brew install portaudio` 用不到，只是多餘；`/usr/local/lib` 裡有 portaudio（Intel 版 Homebrew、自己編的）才會換掉自帶的。不裝 Homebrew 的 portaudio（修正 redesign §9），清單第 7 步確認用的是自帶那份。soundfile 也自帶 libsndfile。
- **麥克風權限**：daemon 從不開音訊裝置（ASR/TTS 處理的是 WebRTC 送來的音訊），所以 LaunchAgent 不需要麥克風權限。語音 bridge 是你在終端機跑的 client（`uv run python -m dollos.voice.bridge`），macOS 把權限算在終端機 app 上，第一次會問。被拒的麥克風送的是全零，不是錯誤：bridge 開始後檢查前 2 秒，全是 0 就以 exit 2 結束，訊息寫「麥克風被拒或靜音：系統設定 › 隱私權與安全性 › 麥克風 › <你的終端機>」。
- **裝置**：`--mic-device`（`voice/bridge/__main__.py:51-54`）和新的 `--speaker-device` 接受編號或名稱片段（sounddevice 支援），因為 macOS 的編號在插拔後會變。麥克風 16 kHz、喇叭 48 kHz（`mic.py:38-45`、`speaker.py:20-24`），由 CoreAudio 轉換；M8 在 Mac 上確認。用 AirPods 當麥克風會把輸出切成低音質的通話模式，建議用內建麥克風。

### §3.5 耐久：pidfile 和原子寫入

- **pidfile**（`wal/pidfile.py:14-48`）：今天用 `os.kill(pid, 0)` 判斷另一個 daemon 在不在。macOS 的 pid 到 99,999 就繞回、每次開機從小的開始，舊 pidfile 的 pid 很容易被你的別的 process 用掉，daemon 就拒絕啟動，KeepAlive 每 10 秒重試一次，永遠起不來。改成兩邊同一套：開 `daemon.pid` 並在整個生命週期握 `fcntl.flock(LOCK_EX | LOCK_NB)`。鎖被占 → 「另一個 daemon 在跑」（明確錯誤）。拿到鎖之後：內容有 pid → DIRTY（上次沒乾淨收尾），空的或檔案不在 → COLD；接著寫自己的 pid，只給人看。乾淨收尾時清空內容再放鎖，不刪檔：刪檔再重建會讓兩個 process 各鎖住不同的 inode，同時起兩個就可能都成功。process 死了 kernel 會放掉鎖。
- **原子寫入**：七個地方寫 tmp 再 rename，都沒有 fsync：`mind/mind_state.py:191-233`、`tools.py:41-46`、`schedule.py:62-79`、`mind/name_aliases.py:113-117`、`mind/evolution.py:112-115` 和 `:365-367`、`wal/perception_log.py:103-106`。ext4 的 `auto_da_alloc` 會在這種 rename 前把資料寫下去，APFS 不會，而且 macOS 的 `os.fsync` 不清磁碟快取（要 `F_FULLFSYNC`）。斷電或 kernel panic 後 `mind_state.json` 可能是空的。加一個 `durable_replace(path, data)`：寫 tmp → flush → darwin 用 `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)`、linux 用 `os.fsync(fd)` → `os.replace`。結果永遠是舊檔或新檔。Apple SSD 上每次幾到幾十 ms，可以接受。WAL 的 append（`perception_log.py:54-68`）照它的說明（`:12`）不 fsync；記憶 markdown 的 append（`tools.py:168-174`、`:204-208`、`memory_writer.py:43`、`:86-91`）也不 fsync：斷電可能少掉或弄壞最後幾行，不會整個檔變空。`fts.db` 可以從 markdown 重建，不設 `PRAGMA fullfsync`。
- **壞掉的自己不能變成冷啟動**：`load_state` 遇到壞檔會改名成 `.corrupt-<epoch>` 再丟例外（`mind_state.py:244-258`）。但 KeepAlive（和 Linux 的 `Restart=on-failure`）一重啟，檔案已經不在，`load_state` 回一個全新的 MindState（`:275-276`），她的 mood、energy、iter_count 就默默歸零。改成：檔案不在、但旁邊有 `mind_state.json.corrupt-*` 時也丟 `MindStateLoadError`，訊息寫要你把它還原或刪掉。代價：在你處理之前，launchd 每 10 秒重啟一次並寫一行錯誤。

### §3.6 依賴與安裝

- 現在的 `uv.lock` 已經涵蓋 macOS arm64 + cp313：96 個適用的基本依賴裡 94 個是 wheel，另外兩個是純 Python（jieba 只有 sdist；luxtts-onnx 是 git 來源）。最高要求 macOS 14（onnxruntime 1.24.4、av 16.1.0）。aiortc 的 av（自帶 FFmpeg）、pylibsrtp、google-crc32c、cryptography 都是 wheel；不需要 Homebrew 的 ffmpeg 或 libsrtp。
- `[tool.uv] find-links`（k2-fsa 的 piper-phonemize 頁）保留：PyPI 的 piper-phonemize 沒有 macOS wheel。
- `gpu` extra 的 6 項加上 `; sys_platform == 'linux'`，再加 `[tool.uv] required-environments = ["sys_platform == 'darwin' and platform_machine == 'arm64'", "sys_platform == 'linux' and platform_machine == 'x86_64'"]`。之後在 zenith 上 `uv lock`，只要某個依賴丟掉 Mac 的 wheel 就會直接失敗，而不是到 Mac 上才發現。在 scratch 試過：lock 只多了 marker，版本不變。
- 這次不升級 lock（`uv lock --upgrade` 會把 sherpa 的 ORT 配對一起拖走）。升級另做一步。
- Mac 上的安裝：Xcode Command Line Tools（`git`，luxtts-onnx 是 git 來源）、uv（官方安裝腳本或 `brew install uv`）、`uv sync` 不加任何 extra。Python 3.13 由 uv 照 `.python-version` 裝。絕不用 `--extra gpu` 或 `--all-extras`；環境裡沒有 torch。
- `docs/roadmap.md:410` 說 onnxruntime 有 `<1.25` 上限，pyproject 裡已經沒有；只有 lock 在固定 1.24.4。記在這裡，不在本文改。

### §3.7 網路

- **Tailscale**：Mac 用 Standalone 版（官方建議；不要同時裝兩種）。Mac 和 Strata 主機都在 Tailscale 後台關掉 key 到期，否則 180 天後 LLM 連線會默默斷掉。GUI 版要登入後才啟動，跟 LaunchAgent 一樣，所以不衝突。
- **Local Network 隱私**（macOS 15 起，Apple TN3179）：LaunchAgent 連「區網」位址要使用者允許（launchd daemon、root、從終端機或 SSH 跑的程式自動允許，agent 不算）；短命的 process 根本不會跳詢問（FB16131937），daemon 啟動檢查失敗就結束正是這種情況。TN3179 的「區網」是 Wi‑Fi、乙太網路這類可廣播介面上的網段，VPN 不算，所以走 Tailscale utun 的 `base_url = "http://100.110.158.36:8080"`（`config.example.toml:9`）不需要任何授權。規則：daemon 由 launchd 跑時，LLM 位址只能是 Tailscale 的 100.x 或 MagicDNS 名稱，`install` 會拒絕看起來是區網的 IP（§3.2）。
- **語音的 WebRTC**：aioice 收集 host candidate 時排除 `127.0.0.1`（`aioice/ice.py:81-92`），所以同一台 Mac 上的語音 bridge 和 daemon 是用 Wi‑Fi/乙太網路的位址或 Tailscale 的 100.x 互連。daemon 送到 Wi‑Fi/乙太網路位址的 ICE 檢查算區網操作，第一次可能跳一個以 python 為名的區域網路詢問；Tailscale 那一對不用。不允許的話只剩 Tailscale 那一對，Tailscale 沒連時語音就連不上。macOS 防火牆如果開著，aiortc 綁在非 loopback 的 UDP 也可能問「接受連入連線」。本文不改 aiortc，只寫明；清單第 15 步記下有沒有跳詢問、選中的是哪一對 candidate。
- **IPC** 照舊綁 `127.0.0.1:9876`（`config.example.toml:17-19`、`ipc/server.py:77`）。在 loopback 上 listen 不需要權限，也不會跳防火牆。
- **Strata 連不上時**：啟動檢查（`kernel.py:1170`）照舊 fail closed，daemon 以非 0 結束，launchd 每 10 秒重試，直到連得上（醒來後 Tailscale 需要一點時間）。本文不改這個行為，只把它寫明。

### §3.8 睡眠與保持清醒

**今天的問題**（zenith 從不睡，所以一直沒出現）：
- 日記排程算一次牆上時間的差，然後一次等完（`kernel.py:1060-1085`）。asyncio 的計時用 monotonic 時鐘，macOS 睡眠時它可能停住，所以 21:00 睡到隔天 07:00，23:00 的日記可能在 09:00 才寫，也沒有任何標記。
- 排程只觸發「現在前後一分鐘內」的項目（`kernel.py:1087-1103`），睡著時到期的提醒會被默默跳過。
- Awoke 只在 daemon 啟動時送（`kernel.py:1182-1196`）；process 撐過睡眠，醒來時沒有 Awoke，但 redesign §9 寫了「醒來的 Awoke 會說離線多久」。

**設計**（兩個 OS 同一份 code，只有「睡了多久」的時鐘依平台查表）：
- 一個 30 秒的 `HostClock` ticker 讀核心給的「主機累計睡了多久」，依平台查表（§2）：darwin = `CLOCK_MONOTONIC − CLOCK_UPTIME_RAW`（`man clock_gettime`：前者睡眠時照走，後者不走），linux = `CLOCK_BOOTTIME − CLOCK_MONOTONIC`。相鄰兩次 tick 之間這個值增加超過 5 秒，就是主機睡過，增加量就是 `offline_s`。不比牆上時間，所以 NTP 調時間、event loop 卡住都不會被當成睡眠。它送出 `Awoke{reason: "woke", offline_s}`，並在 `data/telemetry/host_events.jsonl` 寫 `slept`/`woke`。
- 日記排程改成每 30 秒看一次牆上時間：在 `[目標, 目標 + 10 分]` 之內、當天還沒送過 DiaryMoment 才觸發。「送過」記在 `host_events.jsonl`，所以 daemon 在窗內重啟不會送第二次（今天的 code 在窗內重啟會直接跳到明天）。醒來時已經過了這個窗，就不補寫，記一筆 `diary_missed{date, reason: "host_asleep"}`；daemon 啟動時，最近一個已經過去的窗如果既沒送過也沒記過，記 `diary_missed{date, reason: "daemon_down"}`。這就是 redesign §2 說的「Mac 睡著…的晚上不算進分母，由程式記錄」。
- 睡著時到期的排程項目記成 `schedule_missed`，不補跑（redesign §6.4：「做不到：補跑 Mac 睡著時錯過的提醒」）。

**保持清醒**（Q1）：選「是」的話加 `[host] keep_awake = true`（只有 darwin；Linux 上設了就是設定錯誤）。daemon 啟動時 spawn `/usr/bin/caffeinate -s -w <daemon pid>`：插電時系統不睡（螢幕可以睡），用電池時照常睡，daemon 一死 assertion 就自動放掉，不需要 sudo。用 `pmset -g assertions` 可以看到。筆電闔上蓋子還是會睡，到時候她也睡。選「否」的話她跟著 Mac 睡，上面的偵測讓這件事有紀錄。

**登入和重開機**：LaunchAgent 在 GUI 登入時才啟動。開了 FileVault 的話，重開機（例如系統更新）之後停在解鎖畫面，你登入前她都不在；這沒有官方支援的解法，本文接受，不改用 root daemon。她回來時照舊收到啟動的 Awoke。

---

## §4 測試

### §4.1 Linux 上的單元測試（用 fake 測 macOS 的實作）

- 平台層：`current_host()` 對 linux/darwin/其他值的結果；`check_host` 的拒絕（darwin + `system_pulse.enabled = true`、Linux + `keep_awake = true`）；`load_settings` 不因平台拒絕。
- launchd：plist 產生器的 golden 測試（`plistlib.loads` 來回）、`launchctl` argv builder、`cli.py` 以 `host="darwin"` 和 mock runner 跑每個指令（照 `tests/test_ctl_*.py` 的作法）、install 的兩條拒絕（TCC 路徑、區網 IP）和建 log 目錄。
- 既有的 `tests/test_ctl_*.py` 改成明確用 `host="linux"` 跑：今天它們 patch `systemctl.start` 再呼叫 `main(["start"])`（`tests/test_ctl_cli.py:28-29`），M5 之後在 Mac 上會選到 launchd，沒被 mock 的 `launchctl` 會真的跑，你在 Mac 上跑 pytest 時可能把她停掉。`conftest.py` 加一個 autouse fixture：測試裡呼叫真的 `systemctl._run`／`launchctl._run` 一律丟例外。
- `durable_replace`：monkeypatch `current_host`、`fcntl.fcntl` 和 `os.fsync`，確認 darwin 呼叫 `F_FULLFSYNC`、linux 呼叫 `os.fsync`，而且失敗時目標檔不變。Linux 的 `fcntl` 模組沒有 `F_FULLFSYNC`，測試用 `monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)` 補上（macOS `<sys/fcntl.h>` 的值）。
- keep_awake：mock spawn，確認 darwin 的 argv 是 `/usr/bin/caffeinate -s -w <daemon pid>`，Linux 上設了就被 `check_host` 拒絕。
- pidfile：真的 `flock`（兩個 OS 都能跑）：第二個 acquire 失敗；沒上鎖、內容有 pid → DIRTY；乾淨收尾（內容清空）之後 → COLD；收尾後檔案還在。
- ServiceSupervisor：刪掉 `test_pdeathsig_kills_child_when_parent_dies`（`tests/test_service_supervisor.py:269`），換成 `test_child_exits_on_parent_sigkill`：一個輔助 parent 跑 supervisor 加一個用 watcher 的子行程，SIGKILL parent，子行程 2 秒內消失。兩個 OS 都跑。
- `HostClock` 和日記排程：注入假的「睡了多久」計數和假的牆上時間，測睡過 23:00、睡在窗內醒來、沒睡、event loop 卡住 120 秒（不能算睡）、daemon 在窗內重啟（不送第二次）、daemon 整晚沒跑（`daemon_down`）。darwin 的 `CLOCK_UPTIME_RAW` 只在 darwin 的函式裡用到，Linux 上不會碰到。
- `mind_state`：有 `.corrupt-*`、沒有主檔時 `load_state` 丟例外。

**平台 marker**：`tests/conftest.py` 加 `linux_only`、`darwin_only`，依 `current_host()` 自動 skip 並寫出原因。`tests/test_system_pulse.py::test_poll_now_populates_sample`（`:269`，斷言 `/proc` 的值）標 `linux_only`。

**PortAudio**：`tests/voice/bridge/test_mic.py`、`test_speaker.py` 在 import 時就載入 sounddevice。zenith 沒有 PortAudio，所以今天整套 `pytest` 在收集階段就因這兩個檔 `OSError: PortAudio library not found` 中斷，1814 個測試一個都沒跑。marker 擋不住收集階段的錯誤，所以改成模組開頭 `import sounddevice` 失敗時 `pytest.skip(<原因>, allow_module_level=True)`。這是測試環境的 skip，不是選實作，§2 規則 2 不適用。zenith 上 skip，Mac 上會跑；放在 M1，因為 M1 的完成條件要全套測試綠。

### §4.2 Mac 驗收清單（交給 Mac 上的 Claude Code）

在 repo 根目錄（`~/DollOS/DollOS`）依序跑，每一步都要過：

| # | 指令 | 通過條件 |
|---|---|---|
| 1 | `xcode-select -p && git --version && uv --version` | 三個都有輸出 |
| 2 | `uv sync` | 成功；只 build 了 jieba 和 luxtts-onnx（都是純 Python），沒有編譯 C 擴充 |
| 3 | `uv run python -c "import dollos.kernel"` | 沒有錯誤（M1 之前這步會 ImportError） |
| 4 | `uv run python -c "import sherpa_onnx, onnxruntime, luxtts_onnx, sounddevice, av; print(onnxruntime.__version__, onnxruntime.get_available_providers())"` | `1.24.4`，providers 裡有 `CPUExecutionProvider` |
| 5 | `uv pip list \| grep -iE '^(torch\|nvidia)'` | 沒有輸出 |
| 6 | `uv run python -c "import sqlite3; print([r for r in sqlite3.connect(':memory:').execute('pragma compile_options') if 'FTS5' in r[0]])"` | 清單裡有 `ENABLE_FTS5` |
| 7 | `uv run python -c "import sounddevice as sd; print(sd._libname); print(sd.query_devices())"` | `_libname` 在 `_sounddevice_data/portaudio-binaries/` 底下（用的是 wheel 自帶的 PortAudio）；列得出內建麥克風和喇叭 |
| 8 | `curl -s http://100.110.158.36:8080/v1/models` | 列出 config 裡的 model id |
| 9 | `uv run pytest -q` | 0 failed；skip 的都有寫原因（`linux_only`、需要 torch 的 voice_eval、沒有 cudnn 的 preload 測試） |
| 10 | 把 zenith 的 `config.toml` 和它指到的 bridge／mcp toml 拷過來（都在 gitignore 裡，git 拿不到），設 `[system_pulse] enabled = false`；再 `uv run dollosctl install --daemon-config config.toml --data-root data`、`uv run dollosctl start` | 印出的三個路徑都在 `~/DollOS` 底下；跳出背景項目通知；`enabled` 沒改時 install 明確拒絕 |
| 11 | `uv run dollosctl status`、`uv run dollosctl logs daemon -n 50` | status 顯示 running 和 pid；log 有 `host platform: darwin`、pulse 的 absent 清單、ORT providers，沒有 traceback |
| 12 | config 開 `[bridge]`/`[mcp]` 時：`pgrep -fl dollos` | 看得到 daemon、`dollos.discord_bridge`、`dollos.mcp_server`；開機 60 秒後 log 沒有 BridgeDown/McpDown |
| 13 | `kill -9 <daemon pid>`，等 15 秒，再 `pgrep -fl dollos` | 舊的子行程 2 秒內消失；launchd 在約 10 秒內起了新的 daemon 和新的子行程 |
| 14 | `uv run dollosctl stop`，再 `pgrep -fl dollos` | 沒有任何 dollos process；log 最後是正常收尾，沒有被 SIGKILL |
| 15 | `uv run dollosctl start`，從終端機跑 `uv run python -m dollos.voice.bridge`，允許麥克風，說一句英文 | 一次對話裡 ASR 和 TTS 都在同一個 daemon 裡跑過（兩份 ORT 共存沒出事）；聽到 Gura 的英文聲音；記下有沒有跳區域網路詢問、aiortc log 裡選中的 candidate pair（§3.7） |
| 16 | 在 log 找 luxtts 的 RTF（或照 Gura plan 的指令在 launchd 下量） | RTF ≤ 0.5 |
| 17 | `pmset sleepnow`，2 分鐘後按鍵喚醒，再看 log 和 `data/telemetry/host_events.jsonl` | 有 `slept`/`woke`，她收到 `Awoke{reason: "woke"}`，`offline_s` 約 120（也就驗了 `CLOCK_MONOTONIC` 在睡眠時照走） |
| 18 | （Q1 選是時）`pmset -g assertions \| grep caffeinate` | 看得到 `PreventSystemSleep`，pid 是 daemon 的 caffeinate |
| 19 | 無人看管 72 小時（redesign R1a 的出口條件） | daemon 一直在；log 檔的成長量記下來；pidfile 沒擋過啟動 |

---

## §5 遷移步驟（每一步都能單獨合進 main，Linux 照常能跑）

| 步驟 | 內容 | 完成條件 |
|---|---|---|
| **M1 依賴** | 釘 `sherpa-onnx==1.13.1` + `sherpa-onnx-core==1.13.1`、relock；刪 `_ensure_sherpa_onnx_symlink`；`preload_cudnn` 只在 CUDA 時呼叫；log ORT providers；gpu extra 加 marker、加 `required-environments`；test_mic/test_speaker 的模組層級 skip（§4.1）。zenith 上先刪掉自癒留下的 `.venv/lib/python3.13/site-packages/sherpa_onnx/lib/libonnxruntime.so`（symlink，沒有任何套件的 RECORD 記得它）再 `uv sync` | zenith 上那個檔變成 core 裝的真檔；語音測試和全套測試綠。push 之後（Q2）Mac 清單 1–8 過 |
| **M2 平台層與 pulse 關閉** | `host.py`（`current_host`、`Absent`）；`check_host`（先只有 pulse 那條）接到 `__main__`；`enabled = false` 時不啟動 PulseObserver；scaffolding 的 Body 段和 `[Self pulse]` 那行依 pulse 開關渲染；啟動時的訊號清單和熱乘數原因；`read_loadavg` 改用 `os.getloadavg()`；`conftest.py` 的 `linux_only`／`darwin_only` | 單元測試綠；Linux 上 pulse 行為不變 |
| **M3 Supervisor** | §3.1：stdin 管線、`start_new_session`、`--exit-on-stdin-eof`、`_lifeline.py`（含 5 秒升級）、新的孤兒測試；CLAUDE.md 和 `config.example.toml` 裡講 PDEATHSIG 的說明一起改 | 新測試在 zenith 綠；Linux 上 bridge 和 MCP 照常起停 |
| **M4 耐久** | §3.5：flock pidfile、`durable_replace` 用在七個地方、`.corrupt-*` 擋住冷啟動 | 單元測試綠；zenith 上 daemon 起停正常 |
| **M5 launchd** | §3.2：`ctl/launchd.py`、`ctl/launchctl.py`、依平台選後端、install 的兩條拒絕、`check_host`、建 log 目錄、`status` 印 log 大小；既有 ctl 測試釘 `host="linux"`、真 runner 的防護（§4.1）；CLAUDE.md 的 dollosctl 段加 launchd | 單元測試綠；Linux 的 `dollosctl` 輸出不變 |
| **M6 睡眠** | §3.8：`HostClock`、日記改成看牆上時間（含 `daemon_down`）、`host_events.jsonl`；Q1 選是時加 `keep_awake` 和它的 `check_host` 那條 | 單元測試綠 |
| **M7 語音 bridge** | §3.4：前 2 秒全零就結束並說明、`--mic-device`/`--speaker-device` 接受名稱 | 單元測試綠（Mac 上的清單第 15 步在 M8 驗） |
| **M8 Mac 驗收** | 在 Mac 上跑完 §4.2；結果寫成一頁 `docs/research/` 筆記 | 清單 1–19 全過 |

M1 一定先做（不然 Mac 上什麼都跑不起來），接著 M2（M4–M6 要用它的 `host.py`），M8 一定最後；M3–M7 彼此不相依。全部做完約 1 週，符合 redesign R1a 的預估。之後（不在 v1）：Mac 的 pulse 讀取器，照 §3.3 的表；筆電才加電池和閒置。

**M8 的前置條件**（都不在本文實作）：
- redesign R1a 的「取消註冊 Shell、SpawnWorkflow、SpawnMonitor」已在 main（§3.1：它們的子行程在 Mac 上沒有人收）。
- zenith 上的 daemon 已停：同一個 Discord bot token 只能有一個 bridge，她也只能有一份記憶。
- Mac 的 `data/` 是照 redesign R1a「搬記憶」搬過去的那份。搬之前在 zenith 上確認 `find data | tr A-Z a-z | sort | uniq -d` 沒有輸出：APFS 預設不分大小寫，只差大小寫的檔名會互相蓋掉。

---

## §6 風險

- **完全沒在 Apple 硬體上跑過**：依賴的結論來自 wheel 標籤、wheel 內容和 Mach-O load command。M8 的第 2–7 步是第一次真的驗證。
- **兩份 ORT 1.24.4 在同一個 process**：sherpa-onnx-core 自帶一份，onnxruntime 一份（luxtts、silero VAD 用）。版本相同，macOS 的 two-level namespace 應該分得開，但沒驗證；清單第 15 步就是在驗這個。Linux 今天其實也是兩份（onnxruntime 的 pybind 模組靜態連了一份，sherpa 透過自癒的 symlink 載入 `capi/libonnxruntime.so.1.24.4`），沒出過事。
- **launchd 下的效能**：`ProcessType = Interactive` 對 CPU/E-core 的實際效果沒有精確文件；RTF 0.27 是在前景、ORT 1.31 量的。清單第 16 步重量。
- **KeepAlive 對訊號 crash 的重啟**：有一則 Apple 論壇回報說 SIGSEGV 之後沒有重啟。清單第 13 步用 `kill -9` 驗。
- **`bootout` 會不會等 process 結束**：沒確認。清單第 14 步驗；如果不等，`restart` 要先等 pidfile 的鎖放掉再 `bootstrap`。
- **log 無上限成長**：launchd 不輪替，子行程也寫同一個檔；設定錯誤或自己的檔案壞了時，每 10 秒會多一段錯誤。72 小時量過再決定要不要輪替。
- **背景項目開關**：你在系統設定把它關掉，`dollosctl` 不會知道；那一項可能只顯示成 python。
- **語音的區域網路權限**（§3.7）：daemon 的 python 沒有正式簽章，TN3179 說這種程式的權限紀錄可能不穩，Python 更新後可能再問一次。
- **未確認的細節**：`kern.memorystatus_vm_pressure_level` 的值、macOS 的 `CLOCK_MONOTONIC` 在睡眠時照走（man page 這樣寫，§3.8 靠它；清單第 17 步驗）、`HIDIdleTime` 在螢幕共享時是否重置。各自在用到之前到 Mac 上確認。
- **16 GB**：daemon、SenseVoice、luxtts（記憶體還沒量）和之後 4–8 GB 的 VM。v1 的 pulse 是關的，記憶體壓力不會提醒你；M8 時用活動監視器記一次。
- **之後升 sherpa-onnx 忘了一起升 core**：import 會因為找不到 `libonnxruntime.<ver>.dylib` 而失敗。`pyproject.toml` 在這兩行旁邊寫註解。

---

## §7 要你決定的事

- **Q1 插電時要不要讓 Mac 保持清醒，讓 Doll 一直醒著？** 建議**要**：daemon 用 `caffeinate -s` 撐住，只在插電時有效、daemon 一停就放掉，不用 sudo（§3.8）。不要的話，她跟著 Mac 睡，晚上睡著時的日記會記成「沒寫，因為主機在睡」。
- **Q2 什麼時候把 main push 上 GitHub，讓 Mac 拿得到 code？** 建議 **M1 合進 main 之後就 push**（main 現在比 origin 多 69 個 commit；`ningyos/DollOS` 是公開 repo，push 了就公開），這樣 Mac 上可以先跑清單 1–8，早點發現沒料到的問題。不想公開的話，Mac 也可以透過 Tailscale 直接從 zenith `git clone`。

---

## §8 對 redesign spec 的修正（隨本文一起審，核可後再改那份）

| redesign 的位置 | 現在寫的 | 改成 |
|---|---|---|
| §3 ASR 列（`:125`）、§8（`:366`） | 「Linux 專用的 `.so` 自癒加平台判斷」 | 加 `sherpa-onnx-core==1.13.1` 依賴、刪掉自癒（只加平台判斷的話 Mac 上 ASR 還是 import 不了） |
| §9（`:391`、`:406`） | `brew install portaudio uv` | 只裝 uv（或用官方腳本）；PortAudio 用 sounddevice 自帶的 |
| §9（`:392`） | 「寫 launchd LaunchAgent（KeepAlive…）」 | `KeepAlive {SuccessfulExit: false}`、`ProcessType Interactive`、`ExitTimeOut 90`，以及本文 §3.2 的兩條 install 拒絕 |
| §9（`:395`） | 「醒來的 Awoke 會說離線多久」 | 由 §3.8 的 `HostClock` 提供（今天的 code 沒有） |
| §4 架構圖（`:162`） | 「macOS 26」 | 「macOS 26 以上（你的是 27.0.1）」 |
| §3 Energy 列（`:98`） | 「code 已經如此，`mind_loop.py:893-897`」 | `mind_loop.py:955-970`；啟動 log 寫明 absent 的原因（§3.3） |
