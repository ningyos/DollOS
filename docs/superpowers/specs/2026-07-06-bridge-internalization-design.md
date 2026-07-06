# ServiceSupervisor — DollOS 服務監督器（第一個服務：Discord Bridge 內化，Option A）— Design

Status: **PROPOSAL** (awaiting user approval). 2026-07-06, **R1 review convergence folded in**,
**§3 reframed bridge-specific → generic ServiceSupervisor（使用者定調：DollOS = OS，該有一等服務監督器 / watchdog / task-manager；bridge 是第一個註冊的服務）**。
Grounded against merged code (`src/dollos/kernel.py`, `discord_bridge/__main__.py`,
`monitor_runner.py`, `shell_runner.py`, `ctl/units.py`, `config.py`,
`perception/system_pulse.py`, `voice/bridge/__main__.py`) — file:line references throughout are
ground truth, not aspiration.

Goal (user-set): 讓 DollOS daemon 自己「生」出並看顧 discord-bridge —— bridge 由 daemon 以
**子行程 (child subprocess)** 方式 spawn／supervise／restart／terminate，daemon 掛了 bridge 一起收，
bridge 崩了 daemon 不受影響。config 不併進 daemon（會太肥）：daemon config 只加一個最小的
`[bridge]` 指標區塊，真正的 `[discord]` token/owner 表留在獨立的 `bridge.toml`。

**架構定調（使用者，2026-07-06）：** DollOS 是 **Doll Operating System**，不是「一個 daemon 程式」——所以
看顧 bridge 的機制**不做成 bridge 專屬的一次性 hack，而是一等的 OS 系統功能**：通用
`ServiceSupervisor`（service manager / watchdog）看顧一個**已註冊的長生命週期服務註冊表**，bridge 是
**第一個**註冊的服務，未來 persistent subagent（取代舊名 Drone）、其他連接器都以同一機制註冊。YAGNI：薄抽象、
形狀通用、v1 只接 bridge、不做 UI。記錄於記憶 `project_dollos_os_system_functions`。

---

## 0. R1 review convergence（2026-07-06）— 兩份對抗審查的收斂結論

兩份獨立審查（**process-lifecycle safety** 與 **coherence/YAGNI**）各自逐行對照了 merged code，
**在四個問題上獨立收斂到同一結論**。這一節記錄 diff 與去向；細節在對應章節。

| 收斂點 | 兩審共同結論 | R1 決議（本版已改） | 章節 |
|---|---|---|---|
| **健康模型的盲區（最關鍵）** | supervisor 只對「process exit」反應，但 bridge 最常見的失敗（**壞/撤銷/過期 token**）**根本不 exit** —— 它要嘛卡死在 `wait_until_ready()`（`__main__.py:230`，login 失敗永不 set READY），要嘛 `discord.LoginFailure`（`Exception` 子類）被 reconnect loop 的 `except Exception`（`__main__.py:157`）吞掉、每 5s 重試到天荒地老。uptime 一直漲、`_consecutive_failures` 恆為 0 → supervisor 判它 healthy。crash-loop 上限形同虛設。 | **修 seam，不只包 lifecycle。** 這是 supervisor-restart vs bridge-reconnect 的核心。加 **一處 surgical bridge 改動**（§2 誠實修正「不動 bridge」宣稱）：把 **fatal**（LoginFailure）從 **transient** 分出，讓它 propagate → 非零退出 → supervisor 看得到、crash-loop 上限咬得住（§3.4a、Task B2）。**沒有這一改，headline「大腦看顧 bridge 生死」對頭號失敗是空頭支票。** | §2, §3.4a, §8, §9-1 |
| **daemon SIGKILL → 孤兒 bridge** | `start_new_session=True` 結構性保證了「無 OS 層 parent-death 連動」；bridge reconnect loop 又永不退出 → 孤兒活到天荒地老、重連上 systemd 重拉的新 daemon → 新 daemon 又 spawn 自己的 bridge → **雙 bridge 撞 token**。使用者現況是**手動跑 daemon（無 systemd cgroup 保險）**，此雷是 live 的。原案把唯一解（pidfile）降級成選配 Task E，且 pidfile 治標不治本（孤兒持有自己的 pid、新 bridge acquire 失敗當 crash 放棄 → 孤兒默默服務、supervisor 卻哭「crashed 5× giving up」自相矛盾）。 | **升為核心：`prctl(PR_SET_PDEATHSIG)`**（`preexec_fn`，Linux-only，本專案就 Linux；argv 是直接子行程無 `uv run` 中介 → PDEATHSIG 監看的正是 bridge 本身）。daemon 一死（含 SIGKILL）→ kernel 立即對 bridge 發訊號 → bridge 自盡，**手動/systemd 兩情境都不留孤兒，且發生在新 daemon 起來前，不給雙 bridge 窗口**。pidfile 降為**額外**防線（manual+supervised 並存）保留選配。 | §3.3, §8, §9-2 |
| **「graceful SIGTERM 乾淨終止」是事實錯誤** | bridge **只攔 `KeyboardInterrupt`(=SIGINT)**（`__main__.py:261`），對 SIGTERM **無 handler** → OS 預設處置＝當場終止、不跑任何 `finally`、不優雅關 Discord gateway。原案 §3.5「SIGTERM 會被 Python 預設 handler 乾淨終止」觀念錯誤（沒有會跑 Python code 的預設 handler），且 grace_s→SIGKILL 幾乎永不觸發（SIGTERM 秒殺）。 | **改送 `SIGINT` 而非 SIGTERM。** SIGINT → `except KeyboardInterrupt: return 0`（`__main__.py:261`）→ `_connect_and_run` 的 `finally`（`__main__.py:248-253`）真的跑、乾淨關 gateway、rc=0。**不需動 bridge、契合 no-touch 原則、且 grace_s 真正有意義**（bridge 若沒在 grace 內收，才升級 SIGKILL）。刪掉「graceful SIGTERM」誤稱。 | §3.5, §5, §8 |
| **`[bridge]` 5 個 restart 旋鈕＝config bloat** | 直接違反使用者「`[bridge]` 只放指標＋enable、別讓 daemon config 變肥」的明文指示；對 localhost 乖巧子行程是把 systemd `StartLimit*` 語意搬進 user-facing config 重造。 | **降為 module-level 常數**（`service_supervisor.py`）。`[bridge]` **只留 `enabled` + `config`**。`retention_days` 也移出（它是 bridge 行為，屬 bridge.toml — §4/§9-4）。真要可調，等實際遇到 crash-loop 再加。 | §3.0, §4 |

外加已修的 lifecycle-safety 硬傷（Review 1 I3/I4/M6/M7、Review 2 M1）：

- **respawn 競態**（I3）：shutdown 期間 stop() 讀到 stale `st.proc`，剛 fork 出的新 bridge 逃過 kill → 孤兒。修：`_spawn` fork 回來後**先檢查 `st.stopping` 再原子指派 `st.proc`**，且 stop() 於 cancel task **後**再讀一次 `st.proc` 補刀（§3.3、§3.5）。
- **`_spawn()` OSError／supervise 迴圈裸奔**（I4 + M1）：`create_subprocess_exec` 在高負載丟 `OSError(EAGAIN/ENOMEM)`，或迴圈本體任何非預期例外，會讓 supervise task 靜默死掉、服務從此無人看顧。修：**整個迴圈本體包 try**，非預期例外當一次 crash 走 backoff+retry，不逃出迴圈（§3.4）。
- **inner-finally 早停「做」而非「選配」**（M6）：daemon 關機的數秒 teardown 期間 bridge 每 5s 敲一個正在死的 daemon。既然 stop() idempotent，就在 runner 群組旁（`kernel.py:1102-1105`）早停一次；outer-finally（`kernel.py:1156`）留最強反孤兒 backstop（§3.6）。
- **start() idempotency guard**（M7）：照抄 `system_pulse.py:341-342` 的 `if st.task is not None: continue`，避免被呼叫兩次 → 兩個 supervise loop → 兩個 bridge（§3.4）。
- **放棄／fatal 要浮現**（Review 1 M8）：crash-loop 放棄與（C1 修好後）fatal-token 退出是「Discord 在線整個掛掉」的終局事件，不可只有一行 log。發一個 **perception 給 Doll**（§8、§9-3）。

**未削弱的兩條硬約束**（使用者明令）：crash-isolation **零流失**（bridge 仍是獨立 OS 行程，PDEATHSIG/SIGINT 都在 OS 層、不引入任何 in-process 耦合，§8）；config **不 bloat**（`[bridge]` 縮到 `{enabled, config}`）。

---

## 1. Problem

今天 daemon 與 bridge 是**兩個各自獨立的 systemd `--user` unit**
（`dollos-daemon.service` + `dollos-bridge.service`，`ctl/units.py:49-87`）。這帶來三個負擔：

1. **兩個 unit 要各自管理。** 使用者要 `install`／`start`／`restart` 兩個服務；`dollosctl` 的
   install CLI 要吃 `--daemon-config` **和** `--bridge-config` 兩個參數（`cli.py`），unit 模板要
   interpolate bridge 的 argv（`units.py:81`）。心智負擔與參數面積都是雙份。
2. **bridge 的生死沒有「大腦」在看。** systemd `Restart=on-failure`（`units.py:82`）會無限重啟，
   沒有 backoff 上限、沒有 crash-loop 偵測、崩潰不會浮現到 daemon 這一側。手動跑
   （`uv run python -m dollos.discord_bridge …`）時則完全沒有看顧者。**但注意**（§0 收斂點一）：
   bridge 頭號失敗（壞 token）根本不 exit，所以「看生死」若只等 process exit 會對它瞎眼 —— 這正是
   為何本設計必須連 bridge 的 fatal-error 分類一起修（§3.4a），而非只包 lifecycle。
3. **雙 bridge 撞 token 的風險沒有守門。** bridge 行程自身**沒有** single-instance guard
   （投查一：`wal/pidfile.py` 只守 daemon）。supervised bridge 與 standalone `dollos-bridge.service`
   若同時在跑，兩個 bridge 共用同一 bot token → Discord 拒絕第二個 gateway session。此雷有兩條
   復活路徑：manual+supervised 並存，以及 daemon-SIGKILL 留下的孤兒（§8）。

**為什麼是 Option A（子行程）而不是 in-process？** py-cord / Discord gateway 的崩潰、memory leak、
或阻塞式呼叫**絕不能碰到 daemon 的 event loop 與 Memory SoT**。crash-isolation 是這次內化的**全部重點**。
把 bridge 塞進 daemon 的 asyncio loop 會讓一個 py-cord 例外或 segfault 直接拖垮 Doll。所以 bridge
維持獨立 OS 行程，daemon 只當它的**看顧者 (supervisor)**，唯一耦合是 `proc.wait()` 這個 await 與
既有的 WS 連線（本來就是 reconnect-based、已隔離）。**好的 OS 隔離 driver，不是把它塞進 kernel。**

---

## 2. Scope

**In scope:**
- 新 `ServiceSupervisor` class（**通用服務監督器**）+ `ServiceSpec` frozen 描述 + 服務註冊表：
  register / start（對每個服務起 supervise task）/ stop（收整個註冊表）/ status（§3.7 task-manager 地基）。
  單一服務的 spawn（含 `PR_SET_PDEATHSIG` 反孤兒）/ supervise（crash 偵測 + backoff 重啟 + crash-loop 上限 +
  healthy-uptime 重置 + 迴圈全體例外防護）/ graceful terminate（**SIGINT**→wait→SIGKILL→reap）。
  **v1 只註冊一個服務：discord-bridge**（bridge 的 `ServiceSpec` 由 kernel 建構）。
- **一處 surgical bridge 改動（§3.4a、Task B2）**：把 fatal 連線錯誤（`discord.LoginFailure`）
  從 transient reconnect 分出，讓它 propagate → 非零退出。**這是讓 supervisor 的「看健康」承諾
  對頭號失敗（壞 token）真正成立的必要條件**（§0 收斂點一）。
- daemon config 新增**最小** `[bridge]` 區塊：**只有** `enabled` + `config` 指標。
- kernel 接線（`__init__` 建 supervisor + 註冊 bridge spec、server 起來後 `start()`、inner finally 早停 +
  outer finally `stop()`）。
- stdout/stderr 導向 daemon 的 journal。
- `dollosctl` 單服務化（P1g 簡化）：砍掉 `dollos-bridge.service`，含 install **與** uninstall 兩條路徑的遷移。

**Non-goals / 明確不做:**
- **不把 `[discord]` 表併進 daemon config。** token / owner_discord_id / owner_guild_only /
  backfill_channels 留在 `bridge.toml`，由 bridge 自己的 `_load_bridge_config` 讀
  （`discord_bridge/__main__.py:96-131`）。daemon 只知道**那個檔案的路徑**。
- **內化只包住 bridge 的「生命週期」，不動它的 Discord 業務邏輯。** reconnect loop（transient 重試）、
  ambient log、forward-all/register-on-first-forward 全部原封不動。**唯一的例外是 §3.4a 那處
  fatal-error 分類**——這是原案「完全不改 bridge 內部」宣稱的**誠實修正**：不改這裡，supervisor
  對壞 token 全盲，headline 承諾落空。argv 語意仍是投查三定版的那組。
- **不做 in-process 整併**（見 §1 crash-isolation 論證）。
- **v1 不做多服務、不做 UI、不暴露 IPC 控制命令。** `ServiceSupervisor` 的介面刻意通用（註冊表 + status），
  但 YAGNI——只註冊 bridge 一個服務、`status()` 只純讀取。未來 subagent/連接器再註冊、`dollosctl status`
  再接（§3.7）。**通用是形狀對，不是現在就長出多服務。**
- 分散式部署（bridge 與 daemon 不同機）**不是**這版目標，但**保留能力**：我們只砍 systemd *unit*，
  bridge 的 `python -m dollos.discord_bridge` entry point 原封不動 —— 想跨機的操作者仍可手動跑
  （見 §9 開放決策 6）。

---

## 3. ServiceSupervisor 設計（通用服務監督器 + SupervisedService）

DollOS 是 **Doll Operating System**，所以這一節設計的不是「bridge 專屬看顧者」，而是一個**通用服務監督器
`ServiceSupervisor`**——DollOS 的 OS 級 service manager / watchdog，看顧一個**已註冊的長生命週期 supervised
service 註冊表**。**v1 只註冊一個服務：discord-bridge**（YAGNI——薄抽象、形狀通用、現在不做 UI），但介面刻意
通用：未來 persistent subagent（取代舊名 Drone）、其他外部連接器都以同一機制註冊。新模組
`src/dollos/service_supervisor.py`。結構上最接近的既有樣板是 `src/dollos/perception/system_pulse.py:337-353`
（`start()`/`stop()`、idempotency guard @341-342），子行程原語沿用 `MonitorRunner`/`ShellRunner`（投查二）。

**與既有 runner 的分工（都是 OS 系統功能，但概念不同）：** `ShellRunner`/`MonitorRunner`/`WorkflowRunner`
是**射後不理的 job spawner**（一次性指令、結果回 event queue）；`ServiceSupervisor` 是**長命服務的守護者**
（spawn→supervise→restart→terminate 一個服務註冊表）。記憶 `project_dollos_os_system_functions` 記此分工。

### 3.0 ServiceSpec / _ServiceState / 註冊表（＋restart 旋鈕＝模組常數）

一個服務由 frozen `ServiceSpec` 描述；監督器為每個服務持一份 mutable `_ServiceState`。restart 旋鈕仍是
模組常數（§0 收斂點四），對所有服務共用預設（要 per-service 差異化再升成 spec 欄位——YAGNI）：

```python
# service_supervisor.py — 模組常數（非 user-facing config；YAGNI，要 per-service 調再升）
_GRACE_S = 5.0             # SIGINT 後等多久才升級 SIGKILL
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0
_HEALTHY_UPTIME_S = 60.0   # 撐過這麼久才死 → 視為非 crash-loop，counter 歸零
_MAX_CONSECUTIVE = 5       # 連續快速失敗幾次後放棄
_RETENTION_DAYS = 30       # bridge ambient-log 保留天數（由 kernel 建 argv 時傳入；見 §4/§9-4）

@dataclass(frozen=True)
class ServiceSpec:
    name: str                                   # 唯一鍵（"discord-bridge"）；log / status() 用
    argv: tuple[str, ...]                        # 已 .resolve() 成絕對路徑的完整 argv（§3.1 由 kernel 建）
    on_gave_up: Callable[[str, int | None], None] | None = None   # (name, rc)：放棄時發 perception（§8）

@dataclass
class _ServiceState:
    spec: ServiceSpec
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    stopping: bool = False
    consecutive_failures: int = 0
    phase: str = "idle"                          # idle|running|restarting|gave_up|stopped（§3.7 status）
```

監督器本體只持註冊表；**「哪些服務該存在／enabled」由 kernel 在註冊時決定，監督器本身 enabled-agnostic**
（保持通用——supervisor 不認識「bridge」這個概念，只認識「一個要看顧的服務」）：

```python
class ServiceSupervisor:
    def __init__(self) -> None:
        self._services: dict[str, _ServiceState] = {}

    def register(self, spec: ServiceSpec) -> None:
        if spec.name in self._services:
            raise ValueError(f"service {spec.name!r} already registered")
        self._services[spec.name] = _ServiceState(spec=spec)
        logger.info("service registered: %s argv=%s", spec.name, spec.argv)  # 可見性（§8）
```

### 3.1 bridge 的 argv 與 daemon WS 推導（由 kernel 建 ServiceSpec；投查三）

bridge 的 argv/WS 推導留在 **kernel**（§3.6），監督器只收「已 resolve 的 argv」，因此 supervisor 對
「這是 bridge 還是別的服務」一無所知（通用）。kernel 建 supervisor 並註冊 bridge（緊鄰
`shell_runner`/`monitor_runner`，`kernel.py:422-430`）；**enabled/config-exists 檢查在此、非 supervisor 內**：

```python
# kernel.py —— 建通用監督器並註冊 bridge（v1 唯一服務）
self.service_supervisor = ServiceSupervisor()
if settings.bridge.enabled:
    if settings.bridge.config is None or not settings.bridge.config.exists():
        logger.error("bridge enabled but config missing (%s) — not registering",
                     settings.bridge.config)               # fail-fast、清楚訊號
    else:
        self.service_supervisor.register(self._build_bridge_spec(settings))
```

`_build_bridge_spec`（kernel-local helper，把「bridge 專屬知識」全留在 kernel、不滲進通用 supervisor）：

```python
def _build_bridge_spec(self, settings) -> ServiceSpec:
    argv = (
        sys.executable, "-m", "dollos.discord_bridge",       # 直接子行程、無 uv run 中介 → §3.3 PDEATHSIG 監看的正是 bridge
        "--daemon", _derive_daemon_ws(settings.ipc),
        "--config", str(settings.bridge.config.expanduser().resolve()),
        "--data-root", str(settings.data.root.expanduser().resolve()),
        "--retention-days", str(_RETENTION_DAYS),            # 模組常數（§4/§9-4）
    )
    return ServiceSpec(name="discord-bridge", argv=argv,
                       on_gave_up=self._emit_bridge_down_perception)   # §8 可見性 / §9-3
```

只有 `--config` 是 bridge 端 required（`__main__.py:74-79`）。路徑全部 `.resolve()` 成絕對路徑，所以子行程
繼承 daemon 的 cwd 也不影響。`--data-root` **必須**與 daemon 同一份 `data/`（投查三 (d)：ambient Discord
語料寫到 `{data-root}/discord/…`，錯了會寫到別棵樹）。argv 是 `sys.executable -m dollos.discord_bridge`
（**直接子行程、無 `uv run` 中介**），§3.3 的 PDEATHSIG 監看的正好是 bridge 本身，不是某個 wrapper。

**daemon WS URL 推導**（`_derive_daemon_ws`）：bridge 要連的是 daemon 的 IPC WS
（`config.py` 預設 `127.0.0.1:9876`）。規則：

```python
host = settings.ipc.host
if host in ("0.0.0.0", "::", ""):   # 綁全介面時 loopback 才連得到自己
    host = "127.0.0.1"
return f"ws://{host}:{settings.ipc.port}"
```

### 3.2 spawn 原語（投查二 (a)(b)）＋反孤兒 PDEATHSIG（§0 收斂點二）—— 對每個服務通用

`_preexec` 對**每個** supervised service 都套（PDEATHSIG 是 OS 級、與服務種類無關；spawn 讀 `st.spec.argv`、
狀態寫 `st`）：

```python
def _preexec() -> None:
    # child 內、exec 之前跑。Linux-only（本專案就 Linux）。
    os.setsid()                                    # 獨立 process group（見下）
    _prctl(PR_SET_PDEATHSIG, signal.SIGINT)        # daemon 一死 → kernel 對子行程發 SIGINT
    if os.getppid() == 1:                          # race guard：fork 後、prctl 前 daemon 已死
        os._exit(0)                                # 此時 PDEATHSIG 已錯過，自盡防孤兒

async def _spawn(self, st: _ServiceState) -> None:
    proc = await asyncio.create_subprocess_exec(   # ← await 點：fork 在此同步發生
        *st.spec.argv,
        stdout=None, stderr=None,                  # 繼承 daemon fds → daemon journal（§6）
        preexec_fn=_preexec,                        # 反孤兒 + 獨立 group
    )
    if st.stopping:                                # (I3) stop() 在我們 fork 期間立了旗
        proc.kill(); await proc.wait()             # 這個新子行程沒被存進 st.proc，就地 reap
        raise _SpawnAborted                        # supervise 迴圈據此跳出（不算 crash）
    st.proc = proc                                 # 原子指派（此行前後無 await）
    st.phase = "running"
    logger.info("service %s spawned pid=%d", st.spec.name, proc.pid)  # 可見性（§8）
```

- **`PR_SET_PDEATHSIG(SIGINT)`**：這是「子行程隨父死」的 OS 原生答案，**連 daemon 被 `kill -9`
  都涵蓋**（finally 跑不到、但 kernel 仍會發 PDEATHSIG）。發 **SIGINT** 而非 SIGTERM，讓孤兒走
  bridge 的 `KeyboardInterrupt` 乾淨 teardown 路徑（與 §3.5 supervisor 主動收的訊號一致）。
  `getppid()==1` 是標準 race guard：涵蓋 fork 完成、prctl 尚未執行、而 daemon 恰在此刻死亡的窗口。
- **不用 `start_new_session=True` 參數**、改在 `_preexec` 內顯式 `os.setsid()`：既然已有 `preexec_fn`，
  把 setsid 也收進去（順序：setsid → prctl → getppid 檢查），語意等價於原 `start_new_session=True`
  （獨立 process-group leader，killpg 能連未來孫行程一起收），與 `monitor_runner.py:176` /
  `shell_runner.py:94` 同精神。
- argv **不含 token**（只含 bridge.toml 的**路徑**），所以 `ps` / journal 都看不到密鑰（§8）。
- **`_spawn` 失敗（`OSError` 等）不在此吞**：交給 §3.4 的迴圈全體 try 當一次 crash 處理（I4）。

### 3.3 supervise 迴圈（per-service）：crash 偵測 + backoff 重啟 + crash-loop 上限（＋全體例外防護）

`start()` 對**註冊表每個服務**起一個 supervise task（各自 idempotent，M7）：

```python
def start(self) -> None:
    for st in self._services.values():
        if st.task is not None:                    # (M7) 已在跑，別開第二個 loop
            continue
        st.task = asyncio.create_task(self._supervise(st), name=f"svc:{st.spec.name}")
```

（**注意**：`enabled=false` 時 kernel 根本沒 register bridge → 註冊表空 → `start()` 是 zero-iteration
no-op，零開銷。「該不該存在」的判斷在 kernel 註冊時，不在 supervisor——保持通用。）

per-service supervise 迴圈 —— **整個迴圈本體包在 try 內**（I4 + Review 2 M1）：`_spawn()` 的 `OSError`、
`os.getpgid` 等任何非預期例外都當「一次 crash」走 backoff，**絕不讓 supervise task 靜默死掉**：

```python
async def _supervise(self, st: _ServiceState) -> None:
    while not st.stopping:
        started_at = monotonic()
        try:
            await self._spawn(st)                   # §3.2（含 _SpawnAborted）
            rc = await st.proc.wait()               # 阻塞到服務死掉（reap 也在此完成）
        except _SpawnAborted:
            break                                   # stopping 中途 fork → 已 reap，收工
        except asyncio.CancelledError:
            raise                                   # stop() 取消 → 交給 stop() 收尾
        except Exception:                           # (I4/M1) OSError 等：當一次 crash
            logger.exception("service %s spawn/wait failed — treating as crash", st.spec.name)
            rc = None
        uptime = monotonic() - started_at

        if st.stopping:                             # (A) killed-by-us on shutdown → 不重啟
            break
        if rc == 0:                                 # (B) clean self-exit（含 SIGINT graceful）
            logger.warning("service %s exited cleanly (rc=0) — not restarting", st.spec.name)
            break
        # (C) crash（含 startup config error、fatal-token 退出 §3.4a、spawn OSError）
        if uptime >= _HEALTHY_UPTIME_S:
            st.consecutive_failures = 0             # 撐夠久才死 → 不是 crash-loop，重置
        st.consecutive_failures += 1
        if st.consecutive_failures > _MAX_CONSECUTIVE:
            st.phase = "gave_up"
            logger.error(
                "service %s crashed %d× consecutively (rc=%s) — giving up until daemon restart",
                st.spec.name, st.consecutive_failures, rc)
            if st.spec.on_gave_up is not None:
                st.spec.on_gave_up(st.spec.name, rc)   # (M8) 發 perception 給 Doll（§8/§9-3）
            break                                   # crash-loop 上限 → 放棄並大聲浮現
        st.phase = "restarting"
        delay = min(_BACKOFF_BASE_S * 2 ** (st.consecutive_failures - 1), _BACKOFF_MAX_S)
        logger.warning("service %s crashed (rc=%s, uptime=%.1fs) — restart in %.1fs (fail #%d)",
                       st.spec.name, rc, uptime, delay, st.consecutive_failures)
        try:
            await asyncio.sleep(delay)              # backoff 期間仍可被 stop() cancel
        except asyncio.CancelledError:
            break
```

三態判定（投查三 (c) 明確落地）：

| 狀況 | rc / 訊號 | 動作 |
|---|---|---|
| **killed-by-us**（shutdown SIGINT，§3.5） | `st.stopping==True` | 不重啟，跳出 |
| **clean self-exit** | `rc==0`（bridge 收到 SIGINT/KeyboardInterrupt → `__main__.py:261` `return 0`；**平常自主運行下幾乎不發生**，因 reconnect loop 對 *transient* 錯無 break） | 不重啟，記 warning |
| **crash** | `rc!=0` 或 spawn 例外（崩潰／startup config 缺 key／**fatal-token 退出 §3.4a**／`OSError`） | backoff 重啟，超上限則放棄 + 發 perception |

**backoff 語意**：指數退避 `base·2^(n-1)` 上限 `max`；**healthy-uptime 重置**（撐過
`_HEALTHY_UPTIME_S` 才死 → 視為非 crash-loop，counter 歸零，等同 systemd 的 StartLimitInterval 語意）；
**crash-loop 上限**：連續 `_MAX_CONSECUTIVE` 次「快速失敗」後**放棄**、記 error、**發 perception**（M8）
—— 這一條專治**啟動期確定性失敗**（bridge.toml 缺 `token` key → `KeyError` → 每次都在啟動期非零退出）
**以及**（有 §3.4a 後）**壞/撤銷 token**。**沒有 §3.4a**，這條上限只咬得到「缺 key」型結構錯，
咬不到「token 存在但無效」——那才是頭號失敗（§0 收斂點一）。

### 3.4a **必要的 surgical bridge 改動**：fatal 連線錯誤 → 非零退出（修 seam）

這是把「supervisor-restart」與「bridge-reconnect」兩個機制**接縫接起來**的關鍵改動（§0 收斂點一）。
現況（`__main__.py` 已核對）：壞 token 有兩條「不 exit」路徑——

1. `discord.run()`（在 `discord_task`，`__main__.py:220`）丟 `LoginFailure`，但主協程正卡在
   `await discord.wait_until_ready()`（`__main__.py:230`），login 失敗永不 set READY → **hang 到永遠**，
   `except Exception`（157）連觸發都不會。
2. 即使不 hang，`LoginFailure` 是 `Exception` 子類 → 被 reconnect loop 的 `except Exception`（157）
   吞掉 → sleep 5s 重試 → **無限刷 Discord login endpoint**（可能被 flag token/IP）。

**改動（`discord_bridge/__main__.py`，兩處，surgical）：**

- 在 `_connect_and_run`：把 `await discord.wait_until_ready()` 改成**與 `discord_task` 競賽**
  （`asyncio.wait({discord_task, ready}, return_when=FIRST_COMPLETED)`）；若 `discord_task` 先結束
  （代表 `discord.run()` 已 raise），**取出並 re-raise 它的例外**，而非傻等 READY。這樣 login 失敗
  會浮出來，不再 hang。
- 在 `run()` 的 reconnect loop：把 **fatal**（`discord.LoginFailure`，以及等價的
  authentication/authorization 硬錯）從 `except Exception` **分出**，`raise` 讓它冒出
  `asyncio.run(run(args))` → `main()` 只攔 `KeyboardInterrupt`（`__main__.py:261`）→ **非零退出**。
  **transient**（`websockets.ConnectionClosed`、Discord 5xx、網路抖動）維持在 `except Exception`
  的 5s reconnect。

**接縫語意（本設計對 §0 收斂點一的答案）：**

- **transient（可由重試自癒）** → 留在 **bridge reconnect loop**（process 存活、supervisor 視為 healthy —— 正確，不該為網路抖動重啟整個行程、也不該丟失 reconnect-backfill 去重）。
- **fatal（重試治不好）** → **bridge 非零退出** → **supervisor** 看到死亡 → backoff + crash-loop 上限。
- **分類就是接縫。** 原案的接縫是壞的：bridge 把 fatal（壞 token）當 transient（永遠重試），supervisor 又只看得到死亡 → 頭號失敗掉進兩個機制之間的盲區。§3.4a 把分類補正，兩個機制才各司其職、不重疊也不留縫。

（此改動屬 Task B2，明列在 §9-1 讓使用者拍板「現在就做」vs「v1 出 watchdog-only 並文件標明盲區」。**傾向：現在做**——否則 headline 承諾空頭。**本改動只碰 bridge，與通用 supervisor 無關**——好審查/好 veto。）

### 3.5 graceful terminate — stop() 收整個註冊表（**SIGINT**→wait→SIGKILL→reap）——**net-new pattern**

投查二指出：daemon→child 的 kill 路徑**全走 SIGKILL**，無 graceful（唯一例外是
`voice/bridge/__main__.py:96` 有 SIGINT/SIGTERM handler；**discord bridge 沒有**）。原案想給 bridge
graceful teardown 是對的，但**選錯訊號**：bridge 只攔 `KeyboardInterrupt`(=SIGINT)，對 SIGTERM 無
handler → SIGTERM 會被 OS 預設處置**當場秒殺**、`finally` 不跑、Discord gateway 不優雅關（§0 收斂點三）。
**改送 SIGINT**：觸發 `__main__.py:261` 的 `except KeyboardInterrupt: return 0`，讓
`_connect_and_run` 的 `finally`（`__main__.py:248-253`，cancel `discord_task`、關 gateway）**真的跑**、
rc=0、grace_s 也**真正有意義**（服務若沒在 grace 內乾淨收，才升級 SIGKILL）。`stop()` 對**整個註冊表**
逐服務收（v1 只有 bridge 一個）：

```python
async def stop(self) -> None:
    await asyncio.gather(*(self._stop_one(st) for st in self._services.values()),
                         return_exceptions=True)

async def _stop_one(self, st: _ServiceState) -> None:
    st.stopping = True                           # 先立旗：supervise 迴圈醒來即不重啟；且擋 §3.2 respawn 競態
    proc = st.proc
    if proc is not None and proc.returncode is None:
        _signal_group(proc, signal.SIGINT)       # ← SIGINT（非 SIGTERM），對整個 process group
        try:
            await asyncio.wait_for(proc.wait(), timeout=_GRACE_S)  # 等它 KeyboardInterrupt 乾淨收（reap）
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)  # 逾時才硬砍整組
            await proc.wait()                    # 一定 reap → 不留 zombie
    if st.task is not None and not st.task.done():
        st.task.cancel()
        await asyncio.gather(st.task, return_exceptions=True)
    # (I3) cancel task 後再讀一次：若 stop 期間 supervise 剛好 fork 了新子行程，補刀
    late = st.proc
    if late is not None and late.returncode is None:
        _signal_group(late, signal.SIGKILL)
        await late.wait()
    st.phase = "stopped"

def _signal_group(proc, sig) -> None:            # killpg + ProcessLookupError 守（既有慣例）
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass
```

- 因為每個服務自成 session（§3.2 `os.setsid()`），一律 `os.killpg(os.getpgid(pid), …)` 對**整組**
  下訊號，且用 `ProcessLookupError` 守（`monitor_runner.py:118-122` / `shell_runner.py:100-108` 同慣例）。
- **一定 `await proc.wait()` reap** —— 避免 zombie（`monitor_runner.py:192-202` /
  `shell_runner.py:148-155` 的 kill-then-wait 慣例）。
- **idempotent**：`st.proc is None` 或已退出時，kill 段整段跳過；可被安全呼叫多次（§3.6 需要 —— inner
  finally 早停 + outer finally backstop 會呼叫兩次）。
- **respawn 競態雙保險**（I3）：`st.stopping` 先立旗讓 §3.2 `_spawn` 中途 abort；末尾再讀一次
  `st.proc` 補刀 fork-在-cancel-窗口的漏網新行程。

### 3.6 kernel 接線點（投查一）

**spawn hook：** `await self.server.start()`（`kernel.py:1032`）**回傳後**呼叫
`self.service_supervisor.start()`。`server.start()` 一回傳，WS 就真的在收連線
（`ipc/server.py:76-78`），bridge 可連。實務上**順序其實無所謂**——bridge 的 `run()` 是無窮
reconnect loop（5s 間隔，`__main__.py:153-167`），第一次連不上只是「第 1 圈」。但仍在 server 起來後才
`start()`（省掉一次白費的 failed-connect）。服務是**子行程不是 asyncio task**，所以必須在 1032 之後
`start()`（可放進背景 task 群組 `kernel.py:1061-1073` 一起起）。

**terminate hook：兩個呼叫點（皆 idempotent stop()）——不再是「outer-only + 選配 inner」。**

1. **inner finally 早停（`kernel.py:1102-1105` runner 群組旁）——「做」而非選配（M6）。**
   這是**常態 graceful 路徑**：daemon 關機時 mind loop 已起、走到 inner finally，此處 `stop()`（對 bridge
   發 SIGINT），讓它**趁 daemon 還大致健在時乾淨關 gateway**。否則 `server.stop()`（1081）先斷了 WS，
   bridge 會在 1081→1156 這數秒 teardown（memsearch.close、keeper/skeptic 收尾）期間每 5s 敲一個正在死的
   daemon。
2. **outer finally backstop（`kernel.py:1156`，`self._pidfile.release()` 正前方）——最強反孤兒保證。**
   若 outer try-body 在 spawn 之後、進入 inner try（`kernel.py:1078`）之前就丟例外（例如
   `_replay_wal()` 在 **`kernel.py:1035`** 失敗），**inner finally 根本不會跑** → 少了呼叫點 1。故必須
   在唯一保證會執行的 outer finally 也 stop() 一次。常態下這是 no-op（已在呼叫點 1 收掉，idempotent）。

> 兩點都放。呼叫點 1 給常態 graceful + 消 WS churn；呼叫點 2 給 init-中途-崩潰的反孤兒 backstop。
> `stop()` idempotent 使雙呼叫安全。**加上 §3.2 的 PDEATHSIG**，連 daemon 被 SIGKILL（跑不到任何
> finally）都不留孤兒——三層一起構成「daemon 死 → bridge 必死」的完整保證。

### 3.7 status() —— task-manager 地基（§0 使用者定調）

DollOS = OS → 該能「列出在跑的服務」。這是 task manager 的**第一塊地基**，也是未來 `dollosctl status`
顯示在線通道、Doll 自我感知「我有哪些在線服務」的接口。v1 只是**純讀取**（無 UI、無 IPC 命令暴露——YAGNI）：

```python
def status(self) -> list[dict]:
    return [
        {"name": st.spec.name, "phase": st.phase,
         "pid": st.proc.pid if st.proc and st.proc.returncode is None else None,
         "consecutive_failures": st.consecutive_failures}
        for st in self._services.values()
    ]
```

形狀對了，未來接 `dollosctl status`（§9-5）／perception／Doll 自我感知即可，不用改 supervisor 內部。
v1 不接任何消費端，只暴露方法 + 單元測試——避免長出沒人用的 UI（YAGNI）。

---

## 4. `[bridge]` config schema（最小 = 只有指標 + 開關）

daemon config（`config.py`）新增一個**最小**區塊：**只有 `enabled` + `config`**（§0 收斂點四：
restart 旋鈕已降為 §3.0 的模組常數；`retention_days` 亦移出——見 §9-4）。鏡射 `SystemPulseConfig`
（bool 形狀，`config.py:130-136`）+ `CharacterConfig`（Path + `_expand_user` validator，`config.py:82-92`）：

```python
class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False                 # opt-in；預設關 → 零開銷
    config: Path | None = None            # 指向獨立 bridge.toml（enabled 時 required）

    @field_validator("config", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _require_config_when_enabled(self) -> "BridgeConfig":
        if self.enabled and self.config is None:
            raise ValueError("[bridge].enabled=true 需要 [bridge].config 指向 bridge.toml")
        return self
```

在 `Settings`（`config.py:247-268`）註冊：

```python
bridge: BridgeConfig = Field(default_factory=lambda: BridgeConfig())
```

`Settings` 層是 `extra="forbid"`（`config.py:248`），但因為 `BridgeConfig` 有 default_factory，
沒宣告 `[bridge]` 也合法（等同 `enabled=false`）。TOML 形狀（**就這三行**）：

```toml
[bridge]
enabled = true
config  = "bridge.toml"     # 相對路徑會 expanduser，argv 建構時再 resolve 成絕對
```

**明確不動** bridge 自己的 `[discord]` 表 —— 那留在 `bridge.toml`。daemon config 只多這兩個 key
（指標 + 開關），**忠實符合使用者「只放指標＋enable、別變肥」的硬約束**。restart 行為、retention
都是**行為常數/bridge 行為**，不進 user-facing daemon config（YAGNI；要調再升級成 config —— §9-4/§9-5）。

---

## 5. 三態語意總表

| 事件 | 偵測 | supervisor 反應 |
|---|---|---|
| daemon 啟動、`enabled=true`（bridge 已 register） | `start()` after `server.start()` | 對註冊服務 spawn（含 PDEATHSIG），開始看顧；`phase=running` |
| daemon 啟動、`enabled=false`（bridge 未 register） | `start()` | 註冊表空 → zero-iteration no-op（零開銷） |
| 服務崩潰（rc≠0 / spawn 例外） | `proc.wait()` 回傳或 §3.3 try 攔到、`stopping==False` | backoff 重啟（`phase=restarting`）；連續超上限 → 放棄（`phase=gave_up`）+ error + **perception** |
| **fatal 連線錯誤（壞 token，§3.4a）** | bridge **非零退出**（不再 hang/無限重試） | 走 crash 路徑 → backoff → 超上限放棄（config typo 場景正確） |
| 服務乾淨自退（rc==0） | 同上、rc==0 | 不重啟、記 warning（自主下幾乎不發生） |
| daemon 關機（graceful） | inner finally 早停 + outer finally backstop | 逐服務 **SIGINT**→wait `_GRACE_S`→SIGKILL→reap；`phase=stopped`；不重啟 |
| **daemon 被 SIGKILL（跑不了 finally）** | kernel 發 **PDEATHSIG**（§3.2） | 子行程收 SIGINT 自盡 → **不留孤兒、無雙 bridge** |
| 服務撐過 `_HEALTHY_UPTIME_S` 才死 | uptime 判斷 | 重置該服務 failure counter（非 crash-loop） |
| 查詢在線服務 | `status()`（§3.7） | 回每服務 `{name, phase, pid, consecutive_failures}`（task-manager 地基） |

---

## 6. stdout/stderr 處理（投查二 (d)）

**繼承 daemon 的 fds**（`stdout=None, stderr=None`）—— bridge 的輸出直接寫到 daemon 的
stdout/stderr，在 `systemd --user`（`dollos-daemon.service`）下就落進 **daemon 自己的 journal**。
所以 `dollosctl logs daemon -f` 一次看到 daemon + bridge 兩邊的 log。

**為什麼不用 PIPE：** bridge 長生命週期又話多，未被抽乾的 OS pipe buffer（~64 KB）會填滿 →
**卡死子行程**。monitor/shell runner 之所以能用 PIPE，是因為它們有專職 drain（monitor 的 readline
迴圈 / shell 的 `communicate()`）。supervised service 沒有那個 drain task，所以 inherit 是零死鎖、零額外
程式碼、且與今天 standalone bridge 的行為一致（bridge → journal）的最低風險選擇。

代價：bridge 用 Python `logging`、daemon 用 structlog，兩種格式在 journal 交錯 —— 可接受（都是逐行
文字）。若日後想把 bridge 每行 tag 後 re-emit 進 daemon 的 structlog，改用 PIPE **並**加一個
MonitorRunner 式 readline forwarder task（見 §9 開放決策 5）—— 但**絕不可 PIPE 而無 drain**。

---

## 7. dollosctl 單服務化（P1g 簡化）＋失去的能力（誠實記帳）

daemon 現在自己看顧 bridge，`dollos-bridge.service` 這個獨立 unit **可以整個砍掉**。

**改動：**
- `units.py`：刪 `render_bridge_unit`（`units.py:67-87`）；`UnitParams` 拿掉 bridge-only 欄位
  （`bridge_config`、`daemon_ws`、`retention_days`）。`render_daemon_unit` 不變。
- `cli.py`：`install` 拿掉 `--bridge-config`（bridge config 路徑改由 daemon config 的
  `[bridge].config` 指定）；只寫 `dollos-daemon.service` 一個檔。`logs bridge` 子命令移除
  （bridge log 現在在 daemon journal，文件標注用 `logs daemon`）。`start`/`stop`/`restart` 變單 unit。
- **遷移（既有安裝）—— install 與 uninstall 兩條路徑都要清（Review 2 I4）：**
  - `install`：主動 `systemctl --user disable --now dollos-bridge.service` + `rm`（`missing_ok=True` /
    容忍 `SystemctlError`，比照 `cli.py` uninstall 的 teardown-leniency 慣例）。
  - `uninstall`：**同樣保留**對 legacy `dollos-bridge.service` 的 `disable --now` + `rm`。否則使用者
    `git pull` 後只跑 `dollosctl uninstall`（沒重跑 install）→ 舊的 **enabled** bridge unit 殘留、
    開機自動起 → 與內化 bridge 撞 token（§1.3 那顆雷）。
  - **這一步至關重要且冪等**：舊 enabled bridge unit 若不清，會與 daemon 內化的 bridge 同時跑 → 雙
    bridge 撞 token。文件明講：**升級必須重跑一次 `dollosctl install`**（或 uninstall→install）才會清掉
    舊 unit；只做 `dollosctl restart` 的人**碰不到遷移碼**。文件同步：`CLAUDE.md` Build/Run 段、
    `docs/dollosctl-smoke.md`、`config.example.toml`。

**失去的能力（Review 2 I5，誠實記帳，非純簡化）：** 現況底層可 `systemctl --user restart
dollos-bridge.service` **單獨重啟 bridge**（改了 token/owner 只想熱載 bridge、不動 daemon）。內化後
bridge 是 daemon 子行程，換 `bridge.toml` 只能**整個重啟 daemon**——而 daemon 重啟遠比 bridge 重
（`kernel.py:1030` memsearch reindex、`_replay_wal`、重發 Awoke perception、dirty-restart 判定）。
這是**真實的能力損失**，不是免費簡化。緩解見 §9 開放決策 5（可選 `dollosctl reload-bridge`：對
supervisor 發訊號/IPC 命令，讓它 cycle 該服務子行程重讀 bridge.toml）——列後續，v1 不做，但設計裡承認缺口。

**保留能力：** 我們只砍 systemd *unit*，`python -m dollos.discord_bridge` entry point 不動 →
想跨機／手動跑的操作者仍可自行拉起（此時務必讓 daemon `[bridge].enabled=false`，避免雙 bridge）。

**使用者當前 setup 註記：** 使用者現正手動跑一個 daemon（`config.gura.toml`）。本次改動只是往該 daemon
config 加 `[bridge]` 兩行；**把 `enabled` 設 true + 指好 `config = "bridge.toml"` + 重啟 daemon**，
bridge 就由 daemon 帶起來。不需要動 systemd（他本來就手動跑）。啟用內化 bridge 時，請確保沒有另一個
standalone bridge 或 `dollos-bridge.service` 同時在跑。

---

## 8. Security / Robustness 分析

**Crash-isolation 不流失（Option A 的重點，使用者硬約束）。** bridge 是獨立 OS 行程：py-cord 例外、
segfault、memory leak 都**碰不到** daemon 的 event loop 與 Memory SoT。唯一耦合是 (1) IPC WS（本就
reconnect 隔離）與 (2) supervisor 的 `proc.wait()` await（bridge 崩潰只是喚醒 supervisor 去重啟）。
**PDEATHSIG／SIGINT 都在 OS 層**，不引入任何 in-process 耦合。無共享記憶體、無共享 GIL、無共享
asyncio loop。與 in-process 相比零 isolation 流失 —— 正是不選 in-process 的理由。§3.4a 那處 bridge
改動只碰**錯誤分類與退出碼**，同樣不觸碰 isolation 邊界。**通用化（ServiceSupervisor）不改變這條**：
每個註冊服務都是獨立 OS 行程，supervisor 只是多管一組而已——好的 OS 隔離 driver、不塞進 kernel。

**孤兒 / zombie 防禦（三層，§0 收斂點二）。**
- **PDEATHSIG（§3.2）—— 主防線**：daemon 一死（**含 `kill -9`**）→ kernel 對子行程發 SIGINT →
  自盡，**發生在新 daemon 起來前 → 根本不給雙 bridge 窗口**。手動跑 daemon（無 systemd cgroup
  保險）也涵蓋——這正是使用者的 live 環境。
- **outer-finally stop()（§3.6）—— graceful backstop**：即使 daemon 初始化中途（`_replay_wal` 等）
  就崩，也保證 kill、不留孤兒。
- **一律 `await proc.wait()` reap → 無 zombie**；`os.setsid()` + `os.killpg` → 關機時連整個 process
  group（含未來孫行程）一起收。
- **殘存邊界（已大幅縮小）**：PDEATHSIG 唯一漏接的窗口是「fork 完成、`_prctl` 尚未執行、daemon 恰在此
  微秒內死」——由 `_preexec` 的 `getppid()==1` 自盡守住（§3.2）。理論上仍有「preexec 全跑完前 daemon
  死」的極窄縫，此時 **pidfile（§9-2，選配 Task E）** 是最後一道：孤兒持有 `bridge.pid`，操作者/新
  bridge 可據此偵測。pidfile 因此降為**額外**防線，不再是唯一解。

**Crash-loop 有界，且對頭號失敗不再瞎眼。** 指數 backoff（base→max）+ healthy-uptime 重置 + 連續 N 次
快速失敗後放棄並記 error **+ 發 perception**（M8）。**關鍵**：配合 §3.4a，「壞/撤銷 token」現在會
**非零退出** → 落入這條上限，而非 hang/無限重試被判 healthy（§0 收斂點一）。缺 §3.4a 時，此上限只咬得到
「缺 key」型結構錯。

**可見性（弱機制 playbook 慣例，三面向）。** 每次 register 記 `name + argv`（INFO）；每次 spawn 記
`name + pid`；每次退出記 `rc + uptime + 重啟決策`；**放棄（終局事件）記 error 並發一個 perception 給
Doll**（`on_gave_up(name, rc)`，§3.0/§3.3）—— 讓 Doll/操作者知道「Discord 在線整個掛了」，不再靜默
（M8；perception 內容見 §9-3）。`status()`（§3.7）再給操作者/未來 dollosctl 一個主動查詢面。稀有／關鍵的
看顧行為對操作者三面向可見。

**注入面。** spawn 用 `create_subprocess_exec`（argv list、無 shell）→ config 路徑等值不會被 shell
解讀，無 shell-injection。`preexec_fn` 只呼叫 `os.setsid` / `prctl` / `getppid`（無使用者輸入）。
bridge.toml 路徑來自 daemon config（操作者所有，非 Doll 經 tool 可寫的 data/ 內容）。`ServiceSpec.argv`
由 kernel 建、supervisor 只轉發，無新增使用者可控輸入面。

**Token 不外洩。** argv 只含 bridge.toml 的**路徑**、不含 token；supervisor 從不讀也不記 token。
`ps` / journal 都看不到密鑰。token 只在 bridge 行程內、由 bridge 的 `_load_bridge_config` 讀。

---

## 9. 開放決策（需使用者拍板）

1. **修 seam：現在就做 §3.4a 的 bridge fatal-error 分類？（強烈建議做）** 這是 supervisor-restart vs
   bridge-reconnect 接縫的核心。**做** → supervisor 的「看 bridge 健康」對頭號失敗（壞 token）真正成立，
   代價是**動一處 bridge 內部**（誠實修正原案「不動 bridge」宣稱；不碰 isolation）。**不做** → v1 出
   **watchdog-only**（只看 process 存活），並在 §1/§8/文件**大聲標明**「偵測不到 connected-but-failing、
   壞 token 會靜默離線」。**傾向：做**（列 Task B2）——否則 headline 承諾空頭。**← 需使用者拍板。**
2. **反孤兒主手段：`PR_SET_PDEATHSIG`（本版已採為核心）＋ bridge-side pidfile（選配 Task E）。**
   PDEATHSIG 擋掉 SIGKILL-孤兒的絕大多數情形（含手動跑）；pidfile 再擋 manual+supervised 並存與
   PDEATHSIG 的極窄理論競態。**傾向：PDEATHSIG 為核心（已納入 §3.2）、pidfile 為選配額外防線。**
   若使用者只想要一個，PDEATHSIG 優先（不動 bridge、擋 SIGKILL）。**← 需確認是否也要 pidfile（Task E）。**
3. **bridge「放棄」/ fatal-token 退出要不要發 perception 給 Doll？（本版傾向：要，但只在終局事件）**
   每次 restart churn 只記 log；但**「連續放棄」與「fatal-token 非零退出」是「Discord 在線整個掛了」的
   終局事件，發一個 perception**（如 `kind="BridgeDown"`，data 含 rc / 連續失敗數）讓 Doll 知情。Doll 修
   不了 config typo，但她該「感知到自己少了一條在線通道」（對齊 virtual-being 定位 + 弱機制三面向可見）。
   **← 需拍板 perception 的 kind/內容，或維持 v1 log-only。**
4. **`retention_days` 放哪？（本版已移出 daemon config）** 它是 bridge 的 ambient-log 行為
   （`__main__.py:88-91`），本版讓 kernel 建 argv 時傳模組常數 `_RETENTION_DAYS=30`。若操作者要可調，
   **應放進 `bridge.toml`（bridge-owned，由 bridge 自讀）**，而非 daemon `[bridge]`——契合「daemon 只
   知道 bridge.toml 路徑、不碰 bridge 行為」的分層。**傾向：v1 常數 30；要可調時進 bridge.toml。← 確認。**
5. **要不要補 `dollosctl reload-bridge`（熱載 bridge.toml、不重啟 daemon）／`dollosctl status`（讀 §3.7）？**
   補回單服務化失去的 bridge-only restart 能力（§7 I5）；`status` 是 §3.7 的自然消費端。**傾向：v1 不做**
   （記缺口，§3.7 只留方法）；真的常改 token/owner、或真需要在線服務視圖再補。**← 確認。**
6. **stdout/stderr：inherit（本版採用）vs PIPE+forwarder-to-structlog（每行 tag）。傾向：v1 inherit。**
7. **保留 thin standalone bridge 路徑給分散式部署？** entry point 本就保留，只是不再生 unit。
   **傾向：不生 unit、保留 entry point**，文件註明跨機時 daemon 端 `enabled=false`。

---

## 10. 單概念 Task 拆解（給 SDD）

每個 task 一個概念、一支 subagent，TDD。

- **Task A — `[bridge]` config schema（最小）。** `config.py` 加 `BridgeConfig`（**只有 `enabled` +
  `config`**）+ `Settings` 註冊 + `enabled⇒config` 的 `model_validator` + `_expand_user`。**無 restart
  旋鈕、無 retention_days**（那些是 §3.0 模組常數）。純 config、無行為。測試：預設關、缺 config 的
  enabled 報錯、extra key 被 forbid（含舊的 restart 旋鈕 key 被拒，防遺留）。
- **Task B — `ServiceSupervisor` class（通用核心）。** `service_supervisor.py`：模組常數 +
  `ServiceSpec`（frozen：name/argv/on_gave_up）+ `_ServiceState`（proc/task/stopping/failures/phase）+
  `register()`（重複名 raise）+ spawn（`_preexec`：setsid + `PR_SET_PDEATHSIG(SIGINT)` + `getppid()==1`
  自盡；fork-後 `st.stopping` recheck 原子指派）+ per-service supervise 迴圈（**全體 try 防護**、三態 +
  backoff + healthy-uptime 重置 + crash-loop 上限 + `on_gave_up`）+ `start()`（對註冊表每服務起 task，各自
  **idempotency guard** M7）+ graceful `stop()`（gather `_stop_one`：**SIGINT**→wait→SIGKILL→reap、
  idempotent、末尾 re-read `st.proc` 補刀）+ `status()`（§3.7 純讀取）。用**假子行程**測（spawn 短命
  `python -c` script 觸發：乾淨退出／崩潰／撐久後崩／SIGINT-graceful／SIGINT-逾時-SIGKILL／spawn OSError
  （monkeypatch）／shutdown-中途-fork 競態）。**PDEATHSIG live-assert**：register 假服務 → spawn 子行程 →
  `kill -9` 掉「假父」→ `pgrep` 確認子行程隨之消失。斷言：group-kill + reap（無 zombie）、backoff 序列、
  超上限放棄且觸發 `on_gave_up(name, rc)`、空註冊表時 `start()` no-op、double-`start()` 每服務只一個 loop、
  `register` 重複名 raise、`status()` 回正確 phase/pid。**完全不碰真 Discord、不認識 bridge**（通用測試）。
- **Task B2 —（繫於開放決策 1）bridge fatal-error 分類（§3.4a，surgical）。** `discord_bridge/__main__.py`
  兩處：(i) `_connect_and_run` 把 `wait_until_ready()` 與 `discord_task` 競賽、discord_task 先結束則
  re-raise 其例外（不再 hang）；(ii) `run()` reconnect loop 把 `discord.LoginFailure` 從
  `except Exception` 分出、`raise` → 非零退出。測試：假的 login-failure client → `run()` 非零退出（不
  無限重試）；transient（ConnectionClosed）→ 仍走 5s reconnect。**這是唯一觸碰 bridge 內部的 task**，
  獨立成一支好審查/好 veto。
- **Task C — kernel 接線（含 bridge ServiceSpec 建構）。** `__init__` 建 `ServiceSupervisor()` +
  `_build_bridge_spec`（§3.1：argv/WS 推導、`_emit_bridge_down_perception` 掛 on_gave_up）+
  `settings.bridge.enabled`&config-exists 才 `register`（§3.1）、`server.start()` 後 `start()`（§3.6）、
  **inner finally（`kernel.py:1102-1105`）早停 + outer finally（`kernel.py:1156`）backstop 兩個
  `stop()` 呼叫點**。Live-smoke：真 daemon + 真 bridge.toml + 私人測試伺服器，看 bridge 被帶起、daemon
  graceful 關機時乾淨收掉（gateway 有優雅斷）、**`kill -9` daemon 後 `pgrep -f dollos.discord_bridge`
  為空**（PDEATHSIG 驗證）、無孤兒。
- **Task D — dollosctl 單服務化。** 砍 `render_bridge_unit`、`UnitParams` 瘦身、`cli.py` 拿掉
  `--bridge-config` / `logs bridge`、**install 與 uninstall 都主動清除殘留 `dollos-bridge.service`**
  （遷移，冪等）。更新 `units.py` / `cli.py` 測試 + `CLAUDE.md` Build/Run + `docs/dollosctl-smoke.md`
  （含「升級須重跑 install」）+ `config.example.toml`。
- **Task E（選配，繫於開放決策 2）— bridge-side pidfile guard（額外防線）。** bridge 啟動時 acquire
  `{data-root}/bridge.pid`，已有存活 bridge 則拒啟並非零退出。擋 manual+supervised 並存與 PDEATHSIG
  極窄競態。（觸碰 bridge 內部，與 Task B2 一併評估要不要動 bridge。）
- **Task F — 文件收尾。** `CLAUDE.md` 架構段記「daemon 內化 bridge，經通用 ServiceSupervisor（DollOS OS 級
  服務監督器；PDEATHSIG 反孤兒、SIGINT graceful、fatal-token 會退出）」、`roadmap.md` 加這步、既有安裝
  遷移註記（升級須重跑 install）、`config.example.toml` 的 `[bridge]`（兩行）範例。

依序：**Task A → B → (B2) → C → D**（config 先、通用 supervisor 次、bridge 分類三〔若採納〕、接線四、ctl 五）；
E/F 可並行收尾。B2 與 E 都觸碰 bridge，建議一起拍板（開放決策 1、2）。
