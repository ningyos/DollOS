# ServiceSupervisor — Bridge Internalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** daemon 自己生出並看顧 discord-bridge —— bridge 由 daemon 以子行程 spawn/supervise/restart/terminate,經一個**通用 `ServiceSupervisor`**(DollOS OS 級服務監督器)看顧;daemon 死 bridge 必死,bridge 崩 daemon 不受影響。

**Architecture:** 通用 `ServiceSupervisor` 看顧一個**已註冊的長命服務表**;v1 只註冊一個服務(discord-bridge),spec 由 kernel 建構。bridge 維持獨立 OS 行程(crash-isolation 零流失),唯一耦合是 `proc.wait()` 與既有 WS。反孤兒靠 `PR_SET_PDEATHSIG(SIGINT)`;graceful 靠對 process group 送 `SIGINT`→wait→`SIGKILL`→reap;crash-loop 靠指數 backoff + healthy-uptime 重置 + 連續上限放棄 + `on_gave_up` perception。一處 surgical bridge 改動把 fatal 連線錯誤(壞 token)從 transient reconnect 分出 → 非零退出,讓 supervisor 對頭號失敗不再瞎眼。

**Tech Stack:** Python 3.12 / asyncio / pydantic v2 / py-cord (bridge) / pytest。全在 `src/dollos/`。

**Spec:** `docs/superpowers/specs/2026-07-06-bridge-internalization-design.md`(commit a3dd93a,ServiceSupervisor reframe;R1 對抗審查收斂已折入 §0)。每個 task 開工前**必讀對應 spec 章節**。

## Global Constraints

- **Crash-isolation 零流失**(使用者硬約束):bridge 永遠是獨立 OS 行程。PDEATHSIG/SIGINT/killpg 都在 OS 層,**不得**引入任何 in-process 耦合(無共享 loop/GIL/記憶體)。唯一耦合 = `proc.wait()` await + 既有 reconnect WS。
- **config 不 bloat**(使用者硬約束):daemon `[bridge]` **只有** `enabled` + `config` 兩個 key。restart 旋鈕、retention_days 一律是 `service_supervisor.py` 的**模組常數**,不進 user-facing config。`[discord]` token/owner 表**不併進** daemon config,留在獨立 `bridge.toml`。
- **No fallback**(專案硬約束):不寫任何 fallback/降級。邊界講清楚,失敗就失敗(fail-fast / fail-closed)。
- **一律 reap**:每次 kill 後 `await proc.wait()`,不留 zombie(沿用 `monitor_runner.py:192-202` 慣例)。
- **killpg 守 `ProcessLookupError`**:`os.killpg(os.getpgid(pid), sig)` 一律包 `except ProcessLookupError: pass`(沿用 `monitor_runner.py:118-122`)。
- **Linux-only 前提成立**:本專案就 Linux,`prctl` / `PR_SET_PDEATHSIG` / `os.setsid` 直接用,無需跨平台守。
- **可見性三面向**(弱機制 playbook):register 記 `name+argv`(INFO)、spawn 記 `name+pid`、退出記 `rc+uptime+決策`、放棄記 error **並發 perception**。
- **測試不碰真 Discord**:ServiceSupervisor 測試用假子行程(`python -c` script);bridge fatal 測試用假 login-failure client。真 Discord 只在 live-smoke(Task 4)。
- **commit 前防呆**:每次 commit 前 `git branch --show-current` 確認在 worktree 分支,不在 `main`。

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/dollos/config.py` | Modify | 加 `BridgeConfig`(enabled+config)+ `Settings.bridge` 註冊 |
| `src/dollos/service_supervisor.py` | Create | 通用 `ServiceSupervisor` + `ServiceSpec` + `_ServiceState` + 模組常數 |
| `src/dollos/discord_bridge/__main__.py` | Modify | fatal 連線錯誤(LoginFailure)從 transient 分出 → 非零退出 |
| `src/dollos/kernel.py` | Modify | 建 supervisor + `_build_bridge_spec` 註冊 bridge + start/stop×2 接線 + `_emit_bridge_down_perception` |
| `src/dollos/ctl/units.py` | Modify | 刪 `render_bridge_unit`;`UnitParams` 瘦身 |
| `src/dollos/ctl/cli.py` | Modify | 單服務化:拿掉 `--bridge-config`/`logs bridge`,單 unit start/stop/restart;install+uninstall 清 legacy bridge unit |
| `tests/test_service_supervisor.py` | Create | supervisor 全行為 + PDEATHSIG live-assert |
| `tests/discord_bridge/test_fatal_exit.py` | Create | fatal→非零退出、transient→reconnect |
| `tests/test_config.py`(既有) | Modify | `[bridge]` schema 測試 |
| `tests/ctl/test_*.py`(既有) | Modify | 單服務化 units/cli 測試 |
| `CLAUDE.md` / `docs/roadmap.md` / `config.example.toml` / `docs/dollosctl-smoke.md` | Modify | 文件收尾 |

決策已定(不在此 plan 內再議):**§3.4a fatal 分類 = 做(Task 3)**;**pidfile(舊 Task E)= 不做**;**BridgeDown perception = 發(Task 4)**。

---

### Task 1: `[bridge]` config schema (spec Task A / §4)

**Files:**
- Modify: `src/dollos/config.py`(新增 `BridgeConfig`;在 `Settings` 註冊 `bridge` 欄位 @~268)
- Test: `tests/test_config.py`(既有測試檔;若不存在則 `tests/test_config_bridge.py`)

**Interfaces:**
- Produces: `BridgeConfig(BaseModel)` 具欄位 `enabled: bool = False`、`config: Path | None = None`;`Settings.bridge: BridgeConfig`(有 default_factory,不宣告 `[bridge]` 亦合法)。Task 4 從 `settings.bridge.enabled` / `settings.bridge.config` 讀。

- [ ] **Step 1: 先讀 spec §4** 確認 schema 形狀(只有 enabled+config、enabled⇒config required、extra=forbid、_expand_user)。

- [ ] **Step 2: 寫 failing tests**

在 `tests/test_config.py` 加(或建新檔),鏡射既有 config 測試風格:

```python
import pytest
from pathlib import Path
from pydantic import ValidationError
from dollos.config import BridgeConfig, Settings


def _minimal_settings_dict(tmp_path):
    # LLMConfig + CharacterConfig 是 Settings 僅有的 required 欄位;其餘有預設。
    return {
        "llm": {"base_url": "http://localhost:8001", "model_id": "x"},
        "character": {"pack": str(tmp_path / "pack")},
    }


def test_bridge_defaults_disabled():
    cfg = BridgeConfig()
    assert cfg.enabled is False
    assert cfg.config is None


def test_bridge_enabled_requires_config():
    with pytest.raises(ValidationError, match="config"):
        BridgeConfig(enabled=True)


def test_bridge_config_expands_user():
    cfg = BridgeConfig(enabled=True, config="~/bridge.toml")
    assert cfg.config == Path("~/bridge.toml").expanduser()
    assert cfg.config.is_absolute()


def test_bridge_forbids_extra_keys():
    # 舊的 restart 旋鈕若被誤留在 TOML,必須被拒(防遺留)。
    with pytest.raises(ValidationError):
        BridgeConfig(enabled=True, config="bridge.toml", backoff_max_s=99)


def test_settings_without_bridge_block_defaults_disabled(tmp_path):
    s = Settings.model_validate(_minimal_settings_dict(tmp_path))
    assert s.bridge.enabled is False


def test_settings_with_bridge_block(tmp_path):
    d = _minimal_settings_dict(tmp_path)
    d["bridge"] = {"enabled": True, "config": "bridge.toml"}
    s = Settings.model_validate(d)
    assert s.bridge.enabled is True
    assert s.bridge.config == Path("bridge.toml")
```

> 註:實作前先確認 `_minimal_settings_dict` 的 `llm`/`character` required 形狀與 `config.py` 現況一致(讀 `LLMConfig`/`CharacterConfig`),對不上就照現況修這個 helper——別改 production schema 遷就測試。

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/test_config.py -k bridge -v`
Expected: FAIL(`ImportError: cannot import name 'BridgeConfig'`)

- [ ] **Step 4: 實作 `BridgeConfig`**

在 `config.py`(緊接 `SystemPulseConfig` 之後、`Settings` 之前的區塊,鏡射 `CharacterConfig` @82-92 與 `SystemPulseConfig` @130-136):

```python
class BridgeConfig(BaseModel):
    """Discord-bridge internalization pointer (spec §4).

    最小指標區塊:只有 enabled + 指向獨立 bridge.toml 的 config 路徑。
    真正的 [discord] token/owner 表留在 bridge.toml,daemon 只知道那個檔案的路徑。
    restart 旋鈕 / retention 都是 service_supervisor.py 的模組常數,不在此。
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False                 # opt-in;預設關 → 零開銷
    config: Path | None = None            # 指向獨立 bridge.toml(enabled 時 required)

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

> 確認 `config.py` 頂部已 import `model_validator`(既有 `EnergyConfig`/`BridgeConfig` 等若已用則有;沒有就在既有 `from pydantic import ...` 加上 `model_validator`)。

- [ ] **Step 5: 在 `Settings` 註冊**

`config.py` `Settings` class(@268 `attention` 欄位之後)加:

```python
    bridge: BridgeConfig = Field(default_factory=lambda: BridgeConfig())
```

- [ ] **Step 6: 跑測試確認 pass**

Run: `uv run pytest tests/test_config.py -k bridge -v`
Expected: PASS(6 個)

- [ ] **Step 7: 全 config 測試迴歸**

Run: `uv run pytest tests/test_config.py -q`
Expected: 全綠(沒因新增欄位破壞既有 Settings 測試)

- [ ] **Step 8: Commit**

```bash
git add src/dollos/config.py tests/test_config.py
git commit -m "feat(config): [bridge] enabled+config schema (svc-internalization Task A)"
```

---

### Task 2: `ServiceSupervisor` 通用核心 (spec Task B / §3.0-3.7) — security-bearing,審查用 opus

**Files:**
- Create: `src/dollos/service_supervisor.py`
- Test: `tests/test_service_supervisor.py`

**Interfaces:**
- Produces:
  - `ServiceSpec`(frozen dataclass):`name: str`、`argv: tuple[str, ...]`、`on_gave_up: Callable[[str, int | None], None] | None = None`
  - `ServiceSupervisor`:`__init__()`、`register(spec: ServiceSpec) -> None`(重複 name raise `ValueError`)、`start() -> None`(對每個註冊服務起一個 supervise task,idempotent)、`async stop() -> None`(收整個註冊表)、`status() -> list[dict]`
- Consumes:(無 —— 完全獨立於 kernel/bridge;kernel 在 Task 4 建 spec 傳進來)

- [ ] **Step 1: 讀 spec §3.0–§3.7 + §8**(全文;這是承重核心)。特別確認:三態判定(killed-by-us / clean rc==0 / crash)、backoff 公式 `min(base·2^(n-1), max)`、healthy-uptime 重置、crash-loop 上限、`_preexec`(setsid→prctl→getppid 自盡)、fork-後 `st.stopping` recheck 原子指派、stop() 末尾 re-read `st.proc` 補刀。

- [ ] **Step 2: 寫 failing tests(假子行程,不碰真 Discord)**

`tests/test_service_supervisor.py`。用 `sys.executable -c "<script>"` 當假服務,涵蓋全部行為。範例骨架(實作者補齊斷言):

```python
import asyncio, os, signal, sys, time
import pytest
from dollos.service_supervisor import ServiceSupervisor, ServiceSpec


def _argv(script: str) -> tuple[str, ...]:
    return (sys.executable, "-c", script)


CLEAN_EXIT = "import sys; sys.exit(0)"
CRASH_EXIT = "import sys; sys.exit(3)"
SLEEP_FOREVER = "import time\nwhile True: time.sleep(3600)"
# 收到 SIGINT 乾淨退出 rc=0(模擬 bridge 的 KeyboardInterrupt 路徑):
GRACEFUL_ON_SIGINT = (
    "import time\n"
    "try:\n"
    "    while True: time.sleep(3600)\n"
    "except KeyboardInterrupt:\n"
    "    import sys; sys.exit(0)\n"
)
# 忽略 SIGINT(逼 supervisor 逾時升級 SIGKILL):
IGNORE_SIGINT = (
    "import signal, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "while True: time.sleep(3600)\n"
)


@pytest.mark.asyncio
async def test_clean_exit_not_restarted():
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(CLEAN_EXIT)))
    sup.start()
    await asyncio.sleep(0.5)
    # rc==0 → 不重啟,supervise task 結束
    assert sup.status()[0]["phase"] in ("idle", "running")  # 已自然結束,pid None
    await sup.stop()


@pytest.mark.asyncio
async def test_crash_then_backoff_restart(monkeypatch):
    # 讓 backoff 常數變小以加速;或直接斷言 consecutive_failures 遞增。
    ...


@pytest.mark.asyncio
async def test_crash_loop_gives_up_and_calls_on_gave_up():
    calls = []
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(CRASH_EXIT),
                             on_gave_up=lambda name, rc: calls.append((name, rc))))
    # 用 monkeypatch 把 _BACKOFF_BASE_S / _MAX_CONSECUTIVE 調小加速
    sup.start()
    ...  # 等到超過 _MAX_CONSECUTIVE
    assert calls and calls[0][0] == "t"
    assert sup.status()[0]["phase"] == "gave_up"
    await sup.stop()


@pytest.mark.asyncio
async def test_graceful_sigint_stop():
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(GRACEFUL_ON_SIGINT)))
    sup.start()
    await asyncio.sleep(0.3)
    pid = sup.status()[0]["pid"]
    assert pid is not None
    await sup.stop()  # SIGINT → rc=0 乾淨收
    assert not _pid_alive(pid)  # reaped,無 zombie
    assert sup.status()[0]["phase"] == "stopped"


@pytest.mark.asyncio
async def test_sigint_ignored_escalates_to_sigkill():
    # GRACE 常數 monkeypatch 調小;IGNORE_SIGINT script → 逾時 → SIGKILL → reap
    ...


@pytest.mark.asyncio
async def test_register_duplicate_name_raises():
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(SLEEP_FOREVER)))
    with pytest.raises(ValueError, match="already registered"):
        sup.register(ServiceSpec(name="t", argv=_argv(SLEEP_FOREVER)))


@pytest.mark.asyncio
async def test_empty_registry_start_is_noop():
    sup = ServiceSupervisor()
    sup.start()          # 零註冊 → zero-iteration,不炸
    assert sup.status() == []
    await sup.stop()


@pytest.mark.asyncio
async def test_double_start_single_loop_per_service():
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(SLEEP_FOREVER)))
    sup.start(); sup.start()   # 第二次是 no-op(每服務 idempotency guard)
    await asyncio.sleep(0.2)
    # 只有一個子行程 pid(不是兩個 loop 兩個 proc)
    await sup.stop()


@pytest.mark.asyncio
async def test_spawn_oserror_treated_as_crash(monkeypatch):
    # monkeypatch asyncio.create_subprocess_exec 丟 OSError → supervise 迴圈當一次 crash,
    # 不讓 task 靜默死掉(斷言 consecutive_failures 遞增 / 最終 gave_up)。
    ...


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
```

PDEATHSIG live-assert(獨立測試,用「假父」子行程):

```python
@pytest.mark.asyncio
async def test_pdeathsig_kills_child_when_parent_dies(tmp_path):
    # 假父:一個 python 子行程,它用 ServiceSupervisor spawn 一個 sleeper,印出 sleeper pid,然後自己 sleep。
    # 我們 SIGKILL 假父 → PDEATHSIG(SIGINT)應讓 sleeper 隨之消失。
    fake_parent = (
        "import asyncio, sys\n"
        "from dollos.service_supervisor import ServiceSupervisor, ServiceSpec\n"
        "async def main():\n"
        "    sup = ServiceSupervisor()\n"
        "    sup.register(ServiceSpec(name='s', argv=(sys.executable,'-c','import time\\nwhile True: time.sleep(3600)')))\n"
        "    sup.start()\n"
        "    await asyncio.sleep(0.5)\n"
        "    print(sup.status()[0]['pid'], flush=True)\n"
        "    await asyncio.sleep(3600)\n"
        "asyncio.run(main())\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", fake_parent,
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ},   # 確保 dollos 可 import(PYTHONPATH / 已 installed)
    )
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    sleeper_pid = int(line.strip())
    assert _pid_alive(sleeper_pid)
    proc.kill()                       # SIGKILL 假父 → 跑不到任何 finally
    await proc.wait()
    # PDEATHSIG 應在 kernel 對 sleeper 發 SIGINT → sleeper 死。給它一點時間。
    for _ in range(50):
        if not _pid_alive(sleeper_pid):
            break
        await asyncio.sleep(0.1)
    assert not _pid_alive(sleeper_pid), "PDEATHSIG 未讓孤兒隨父死"
```

> 若 `dollos` 未安裝進測試環境的 site-packages(而是 `src/` layout),假父 script 需要 `PYTHONPATH` 指到 `src`。實作者用 `env={**os.environ, "PYTHONPATH": <src>}` 或依 repo 的 pytest 設定調整。

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/test_service_supervisor.py -v`
Expected: FAIL(`ModuleNotFoundError: dollos.service_supervisor`)

- [ ] **Step 4: 實作 `service_supervisor.py`**(完整程式碼,對照 spec §3.0–§3.7)

```python
"""通用服務監督器 —— DollOS 的 OS 級 service manager / watchdog。

看顧一組已註冊的長生命週期 supervised service:spawn(含 PR_SET_PDEATHSIG 反孤兒)/
supervise(crash 偵測 + 指數 backoff + healthy-uptime 重置 + crash-loop 上限)/ graceful
terminate(SIGINT→wait→SIGKILL→reap)。v1 只註冊一個服務(discord-bridge),但介面通用。

與 ShellRunner/MonitorRunner/WorkflowRunner(射後不理的 job spawner)分工不同:本類是
長命服務的守護者。設計見 docs/superpowers/specs/2026-07-06-bridge-internalization-design.md。
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import os
import signal
from dataclasses import dataclass, field
from time import monotonic
from typing import Callable

logger = logging.getLogger(__name__)

# 模組常數(非 user-facing config;YAGNI,要 per-service 差異化再升 spec 欄位)
_GRACE_S = 5.0             # SIGINT 後等多久才升級 SIGKILL
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0
_HEALTHY_UPTIME_S = 60.0   # 撐過這麼久才死 → 視為非 crash-loop,counter 歸零
_MAX_CONSECUTIVE = 5       # 連續快速失敗幾次後放棄
_RETENTION_DAYS = 30       # bridge ambient-log 保留天數(kernel 建 argv 時取用;見 §4)

_PR_SET_PDEATHSIG = 1      # <sys/prctl.h>


def _prctl_pdeathsig(sig: int) -> None:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0)


def _preexec() -> None:
    """child 內、exec 之前跑(Linux-only)。反孤兒 + 獨立 process group。"""
    os.setsid()                                  # 獨立 group(killpg 能連孫行程一起收)
    _prctl_pdeathsig(signal.SIGINT)              # daemon 一死 → kernel 對本行程發 SIGINT
    if os.getppid() == 1:                        # race guard:prctl 前 daemon 已死
        os._exit(0)                              # PDEATHSIG 已錯過 → 自盡防孤兒


class _SpawnAborted(Exception):
    """stop() 在 fork 期間立旗 → 剛 fork 的新行程已就地 reap,supervise 迴圈據此收工。"""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]
    on_gave_up: Callable[[str, int | None], None] | None = None


@dataclass
class _ServiceState:
    spec: ServiceSpec
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    stopping: bool = False
    consecutive_failures: int = 0
    phase: str = "idle"     # idle|running|restarting|gave_up|stopped


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass


class ServiceSupervisor:
    def __init__(self) -> None:
        self._services: dict[str, _ServiceState] = {}

    def register(self, spec: ServiceSpec) -> None:
        if spec.name in self._services:
            raise ValueError(f"service {spec.name!r} already registered")
        self._services[spec.name] = _ServiceState(spec=spec)
        logger.info("service registered: %s argv=%s", spec.name, spec.argv)

    def start(self) -> None:
        for st in self._services.values():
            if st.task is not None:              # (M7) idempotency:已在跑就跳過
                continue
            st.task = asyncio.create_task(self._supervise(st), name=f"svc:{st.spec.name}")

    async def _spawn(self, st: _ServiceState) -> None:
        proc = await asyncio.create_subprocess_exec(
            *st.spec.argv,
            stdout=None, stderr=None,            # 繼承 daemon fds → daemon journal
            preexec_fn=_preexec,
        )
        if st.stopping:                          # (I3) stop() 在 fork 期間立了旗
            proc.kill()
            await proc.wait()                    # 就地 reap 這個沒被存進 st.proc 的新行程
            raise _SpawnAborted
        st.proc = proc                           # 原子指派(前後無 await)
        st.phase = "running"
        logger.info("service %s spawned pid=%d", st.spec.name, proc.pid)

    async def _supervise(self, st: _ServiceState) -> None:
        while not st.stopping:
            started_at = monotonic()
            try:
                await self._spawn(st)
                rc = await st.proc.wait()
            except _SpawnAborted:
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "service %s spawn/wait failed — treating as crash", st.spec.name)
                rc = None
            uptime = monotonic() - started_at

            if st.stopping:                      # (A) killed-by-us
                break
            if rc == 0:                          # (B) clean self-exit
                logger.warning(
                    "service %s exited cleanly (rc=0) — not restarting", st.spec.name)
                break
            # (C) crash
            if uptime >= _HEALTHY_UPTIME_S:
                st.consecutive_failures = 0
            st.consecutive_failures += 1
            if st.consecutive_failures > _MAX_CONSECUTIVE:
                st.phase = "gave_up"
                logger.error(
                    "service %s crashed %d× consecutively (rc=%s) — giving up until "
                    "daemon restart", st.spec.name, st.consecutive_failures, rc)
                if st.spec.on_gave_up is not None:
                    try:
                        st.spec.on_gave_up(st.spec.name, rc)
                    except Exception:
                        logger.exception("on_gave_up callback raised for %s", st.spec.name)
                break
            st.phase = "restarting"
            delay = min(_BACKOFF_BASE_S * 2 ** (st.consecutive_failures - 1), _BACKOFF_MAX_S)
            logger.warning(
                "service %s crashed (rc=%s, uptime=%.1fs) — restart in %.1fs (fail #%d)",
                st.spec.name, rc, uptime, delay, st.consecutive_failures)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        await asyncio.gather(
            *(self._stop_one(st) for st in self._services.values()),
            return_exceptions=True,
        )

    async def _stop_one(self, st: _ServiceState) -> None:
        st.stopping = True
        proc = st.proc
        if proc is not None and proc.returncode is None:
            _signal_group(proc, signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_GRACE_S)
            except asyncio.TimeoutError:
                _signal_group(proc, signal.SIGKILL)
                await proc.wait()
        if st.task is not None and not st.task.done():
            st.task.cancel()
            await asyncio.gather(st.task, return_exceptions=True)
        late = st.proc                           # (I3) cancel 後補刀 fork-在-窗口的漏網新行程
        if late is not None and late.returncode is None:
            _signal_group(late, signal.SIGKILL)
            await late.wait()
        st.phase = "stopped"

    def status(self) -> list[dict]:
        return [
            {
                "name": st.spec.name,
                "phase": st.phase,
                "pid": st.proc.pid if st.proc and st.proc.returncode is None else None,
                "consecutive_failures": st.consecutive_failures,
            }
            for st in self._services.values()
        ]
```

> 實作者注意:`field` 未用到可移除 import。`_prctl_pdeathsig` 用 ctypes 直呼 libc `prctl`(Python 無標準 `prctl` 綁定);這是 Linux syscall,回傳值忽略(失敗只影響反孤兒的一層,PDEATHSIG 本就是三層防禦之一)。若 repo 已有 `prctl` 封裝(grep 一下),優先沿用。

- [ ] **Step 5: 跑測試確認 pass**

Run: `uv run pytest tests/test_service_supervisor.py -v`
Expected: PASS(全部,含 PDEATHSIG live-assert)。若 backoff 測試太慢,用 `monkeypatch.setattr` 把 `service_supervisor._BACKOFF_BASE_S`/`_MAX_CONSECUTIVE`/`_GRACE_S` 調小。

- [ ] **Step 6: Commit**

```bash
git add src/dollos/service_supervisor.py tests/test_service_supervisor.py
git commit -m "feat(svc): generic ServiceSupervisor (PDEATHSIG anti-orphan, SIGINT graceful, crash-loop cap) (Task B)"
```

---

### Task 3: bridge fatal-error 分類 → 非零退出 (spec Task B2 / §3.4a) — surgical,唯一觸碰 bridge 內部

**Files:**
- Modify: `src/dollos/discord_bridge/__main__.py`(`run()` reconnect loop @154-167、`_connect_and_run()` @224-236、`main()` @256-263)
- Test: `tests/discord_bridge/test_fatal_exit.py`(新檔;若既有 bridge 測試檔更合適則加進去)

**Interfaces:**
- Consumes:(bridge 既有 `run`/`_connect_and_run`/`main`;`discord.LoginFailure` from py-cord)
- Produces:壞/撤銷 token → `main()` 回**非零 exit code(2)**;`_connect_and_run` 對 login 失敗**不再 hang**;transient(ConnectionClosed 等)維持 5s reconnect。

- [ ] **Step 1: 讀 spec §3.4a** 全文,確認兩處改動(wait_until_ready 競賽 re-raise、reconnect loop fatal 分出)+ main() 非零退出。

- [ ] **Step 2: 寫 failing tests**

`tests/discord_bridge/test_fatal_exit.py`。用假 `discord` client(monkeypatch `PycordClient`)注入 LoginFailure。骨架:

```python
import asyncio
import pytest
import discord   # py-cord;bridge 已透過 PycordClient 依賴它

from dollos.discord_bridge import __main__ as bridge_main


@pytest.mark.asyncio
async def test_login_failure_propagates_not_hang(monkeypatch, tmp_path):
    """discord.run() 丟 LoginFailure 時,_connect_and_run 不 hang 在 wait_until_ready,
    而是 re-raise → run() 的 fatal 分支 → 冒出。"""
    # 假一個 client:run() 立刻 raise LoginFailure;wait_until_ready() 永不完成。
    class _FailClient:
        def __init__(self, token): ...
        def on_message(self, cb): ...
        async def run(self):
            raise discord.LoginFailure("bad token")
        async def wait_until_ready(self):
            await asyncio.Event().wait()      # 永不 ready(模擬現況 hang)
        def me_id(self): return 1
        async def fetch_history(self, *a, **k): return []
    monkeypatch.setattr(bridge_main, "PycordClient", _FailClient)
    # 也要假掉 websockets.connect(daemon WS),避免真連線。
    ...
    with pytest.raises(discord.LoginFailure):
        await bridge_main._connect_and_run(_fake_args(tmp_path), "tok", _fake_cfg(), _fake_ambient())


@pytest.mark.asyncio
async def test_run_exits_on_fatal_not_infinite_retry(monkeypatch, tmp_path):
    """run() 收到 fatal 應 raise 出去(不進 5s reconnect)。"""
    async def _boom(*a, **k):
        raise discord.LoginFailure("bad token")
    monkeypatch.setattr(bridge_main, "_connect_and_run", _boom)
    # 若 run 進了無限 reconnect,這個 wait_for 會逾時 → 測試失敗
    with pytest.raises(discord.LoginFailure):
        await asyncio.wait_for(bridge_main.run(_fake_args(tmp_path)), timeout=2)


@pytest.mark.asyncio
async def test_run_retries_on_transient(monkeypatch, tmp_path):
    """transient(非 LoginFailure)→ 仍走 reconnect(不 raise 出去)。"""
    calls = []
    async def _drop(*a, **k):
        calls.append(1)
        if len(calls) >= 2:
            raise KeyboardInterrupt   # 用來讓 run() 退出測試(非 Exception 子類)
        raise ConnectionError("transient drop")
    monkeypatch.setattr(bridge_main, "_connect_and_run", _drop)
    monkeypatch.setattr(bridge_main.asyncio, "sleep", lambda *_: asyncio.sleep(0))  # 免等 5s
    with pytest.raises(KeyboardInterrupt):
        await bridge_main.run(_fake_args(tmp_path))
    assert len(calls) >= 2    # 有重試,沒有第一次就 raise 出去


def test_main_returns_nonzero_on_login_failure(monkeypatch):
    async def _boom(_args):
        raise discord.LoginFailure("bad token")
    monkeypatch.setattr(bridge_main, "run", _boom)
    rc = bridge_main.main(["--daemon", "ws://x", "--config", "/x", "--data-root", "/x"])
    assert rc != 0
```

> 實作者補齊 `_fake_args`/`_fake_cfg`/`_fake_ambient`/websockets monkeypatch。重點斷言是四條:hang→re-raise、fatal→不無限重試、transient→重試、main→非零。

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/discord_bridge/test_fatal_exit.py -v`
Expected: FAIL(現況 hang / 無限重試 / main 回 0)

- [ ] **Step 4: 改 `_connect_and_run`**(@224-236 的 `await discord.wait_until_ready()` 段)

把「傻等 ready」改成「與 discord_task 競賽」:

```python
        try:
            # wait_until_ready() 是「成功連上」訊號;但 login 失敗時它永不完成
            # (discord.run() 會先丟 LoginFailure)。與 discord_task 競賽,discord_task
            # 先結束代表 run() 已 raise → 取出並 re-raise,不再 hang(spec §3.4a)。
            ready = asyncio.ensure_future(discord.wait_until_ready())
            done, _pending = await asyncio.wait(
                {discord_task, ready}, return_when=asyncio.FIRST_COMPLETED
            )
            if discord_task in done:
                ready.cancel()
                exc = discord_task.exception()
                if exc is not None:
                    raise exc
                raise RuntimeError("discord client exited before ready")
            # ready 先完成 → 正常路徑
            if cfg.bot_id is None:
                cfg.bot_id = discord.me_id()
            logger.info("discord connected — backfilling reconnect gap")
            await controller.reconnect_backfill(
                discord.fetch_history, cfg.backfill_channels
            )
            async for raw in ws:
                ...  # (原本的 live-message 迴圈不變)
        finally:
            discord_task.cancel()
            ...
```

> 保留原 finally 的 `discord_task.cancel()` + await 收尾。`ready.cancel()` 在 discord_task 先完成的分支做一次即可(其 await 由 finally 或 GC 處理;實作者可視情況 `await ready` 吞 CancelledError 保持乾淨)。

- [ ] **Step 5: 改 `run()` reconnect loop**(@154-167)把 fatal 分出

```python
    reconnect_delay_s = 5.0
    while True:
        try:
            await _connect_and_run(args, token, cfg, ambient)
        except discord.LoginFailure:
            # fatal:壞/撤銷/過期 token。重試治不好,且會無限刷 login endpoint。
            # 讓它冒出 → main() 非零退出 → ServiceSupervisor 看得到死亡、crash-loop 上限
            # 咬得住(spec §3.4a 接縫)。
            logger.error("discord login failed (bad/revoked token) — exiting for supervisor")
            raise
        except Exception:
            logger.exception(
                "discord bridge connection dropped — reconnecting in %.0fs",
                reconnect_delay_s,
            )
        else:
            logger.warning(
                "daemon connection closed cleanly — reconnecting in %.0fs",
                reconnect_delay_s,
            )
        await asyncio.sleep(reconnect_delay_s)
```

需在檔案頂 import:`import discord`(bridge 已透過 `PycordClient` 依賴 py-cord;確認 import 位置與既有風格一致)。

- [ ] **Step 6: 改 `main()`**(@256-263)顯式非零退出

```python
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except discord.LoginFailure:
        logger.error("fatal: discord login failed — check bot token in bridge.toml")
        return 2
    return 0
```

- [ ] **Step 7: 跑測試確認 pass**

Run: `uv run pytest tests/discord_bridge/ -v`
Expected: PASS(新 4 條 + 既有 bridge 測試迴歸綠)

- [ ] **Step 8: Commit**

```bash
git add src/dollos/discord_bridge/__main__.py tests/discord_bridge/test_fatal_exit.py
git commit -m "fix(bridge): fatal login errors exit non-zero instead of hang/infinite-retry (Task B2, spec §3.4a)"
```

---

### Task 4: kernel 接線 + bridge ServiceSpec 建構 + BridgeDown perception (spec Task C / §3.1, §3.6) — 審查用 opus

**Files:**
- Modify: `src/dollos/kernel.py`(construction @~430、`_build_bridge_spec` + `_derive_daemon_ws` + `_emit_bridge_down_perception` helpers、startup @~1058、inner finally @1104、outer finally @1157)
- Test: `tests/test_kernel_bridge_wiring.py`(新檔;construction/register/derive 的單元測試,不起真 bridge)

**Interfaces:**
- Consumes:`ServiceSupervisor`/`ServiceSpec`(Task 2)、`settings.bridge`(Task 1)、`settings.ipc.host/port`、`settings.data.root`、`service_supervisor._RETENTION_DAYS`
- Produces:daemon 啟動時若 `settings.bridge.enabled` 且 config 存在 → register + spawn bridge;graceful/SIGKILL 關機都不留孤兒;放棄時發 `Perception(kind="BridgeDown")`。

- [ ] **Step 1: 讀 spec §3.1、§3.6、§8、§9-3**(argv/WS 推導、兩個 stop() 呼叫點、perception 內容)。

- [ ] **Step 2: 寫 failing tests**(不起真子行程;測 spec 建構與接線邏輯)

`tests/test_kernel_bridge_wiring.py`:

```python
from pathlib import Path
from dollos.kernel import _derive_daemon_ws   # module-level helper


def test_derive_daemon_ws_loopback_for_wildcard():
    class _IPC: host, port = "0.0.0.0", 9876
    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9876"


def test_derive_daemon_ws_keeps_explicit_host():
    class _IPC: host, port = "127.0.0.1", 9999
    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9999"


def test_build_bridge_spec_argv(tmp_path):
    # 建一個 minimal kernel/settings,呼叫 _build_bridge_spec,斷言 argv 含
    # -m dollos.discord_bridge / --config 絕對路徑 / --data-root 絕對路徑 / --retention-days。
    ...
    spec = kernel._build_bridge_spec(settings)
    assert spec.name == "discord-bridge"
    assert "dollos.discord_bridge" in spec.argv
    assert str((tmp_path / "bridge.toml").resolve()) in spec.argv
    assert "--retention-days" in spec.argv
    # token 不在 argv(只有路徑)
    assert not any("MTM" in a or "token" in a.lower() for a in spec.argv)
```

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/test_kernel_bridge_wiring.py -v`
Expected: FAIL(`_derive_daemon_ws` / `_build_bridge_spec` 不存在)

- [ ] **Step 4: 加 module-level helper + perception emitter**

`kernel.py`(靠近其他 module-level helper / import 區):

```python
from dollos.service_supervisor import ServiceSupervisor, ServiceSpec, _RETENTION_DAYS


def _derive_daemon_ws(ipc) -> str:
    host = ipc.host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"ws://{host}:{ipc.port}"
```

在 `DollOSKernel` 內加(緊鄰 argv/spec 用途):

```python
    def _build_bridge_spec(self, settings) -> ServiceSpec:
        argv = (
            sys.executable, "-m", "dollos.discord_bridge",
            "--daemon", _derive_daemon_ws(settings.ipc),
            "--config", str(settings.bridge.config.expanduser().resolve()),
            "--data-root", str(settings.data.root.expanduser().resolve()),
            "--retention-days", str(_RETENTION_DAYS),
        )
        return ServiceSpec(
            name="discord-bridge", argv=argv,
            on_gave_up=self._emit_bridge_down_perception,
        )

    def _emit_bridge_down_perception(self, name: str, rc: int | None) -> None:
        # 終局事件:Discord 在線整個掛了。Doll 修不了 config typo,但該感知到少了一條在線通道
        # (spec §9-3;virtual-being 定位 + 弱機制三面向可見)。on_gave_up 在 supervise task
        # 內同步呼叫 → 只 put queue、不做重活。
        try:
            self._perception_queue.put(Perception(
                kind="BridgeDown",
                t=time.time(),
                data={"service": name, "rc": rc},
            ))
        except Exception:
            logger.exception("failed to emit BridgeDown perception")
```

> `Perception` / `time` 在 kernel 已 import(見 @1049 Awoke perception 用法)。確認 `sys` 已 import。

- [ ] **Step 5: 建 supervisor + 註冊 bridge**(construction,@430 `monitor_runner` 之後)

```python
        self.service_supervisor = ServiceSupervisor()
        if settings.bridge.enabled:
            if settings.bridge.config is None or not settings.bridge.config.exists():
                logger.error(
                    "bridge enabled but config missing (%s) — not registering",
                    settings.bridge.config,
                )
            else:
                self.service_supervisor.register(self._build_bridge_spec(settings))
```

> `_build_bridge_spec` 是 method,但 construction 期呼叫需要 `self._perception_queue` 已存在(給 `_emit_bridge_down_perception` 綁定)——它只是綁 method reference,不會在 construction 就觸發,所以順序安全。確認 `settings` 在 `__init__` 作用域可取(既有 code 用 `settings.` 直接讀)。

- [ ] **Step 6: 啟動 hook**(@1058 `system_pulse.start()` 附近,server 已 start 之後)

```python
            # ServiceSupervisor:看顧已註冊的長命服務(v1 = discord-bridge)。
            # 必須在 server.start() 之後(bridge 要連 daemon WS)。
            self.service_supervisor.start()
```

- [ ] **Step 7: 關機 hook —— 兩個 idempotent stop() 呼叫點**

(a) inner finally 早停,@1104 `await self.monitor_runner.stop()` 之後:

```python
                await self.service_supervisor.stop()   # 早停:趁 daemon 還健在讓 bridge 乾淨關 gateway
```

(b) outer finally backstop,@1157 `self._pidfile.release()` **正前方**:

```python
        finally:
            await self.service_supervisor.stop()       # backstop:init-中途-崩潰也不留孤兒(idempotent)
            self._pidfile.release()
```

> 若 `service_supervisor` 可能在極早期 init 失敗前就被讀到,outer finally 用 `getattr(self, "service_supervisor", None)` 守(比照 @1111 `_ct` 的 `getattr` 慣例)。實作者判斷:construction @430 在 pidfile @381 之後,但 outer try 從 @1031 起,所以 `self.service_supervisor` 在進 outer try 前已存在 → 直接 `self.service_supervisor.stop()` 安全。保守起見可加 getattr 守。

- [ ] **Step 8: 跑單元測試 + 全 kernel 測試**

Run: `uv run pytest tests/test_kernel_bridge_wiring.py tests/test_kernel*.py -v`
Expected: PASS(接線單元 + 既有 kernel 迴歸)

- [ ] **Step 9: Commit**

```bash
git add src/dollos/kernel.py tests/test_kernel_bridge_wiring.py
git commit -m "feat(kernel): wire ServiceSupervisor + register bridge spec + BridgeDown perception (Task C)"
```

- [ ] **Step 10: Live-smoke（人工,非 CI；記錄於 PR/handoff)**

真 daemon(`config.gura.toml` 加 `[bridge] enabled=true, config="bridge.toml"`)+ 真 `bridge.toml`(已填真 token/owner)+ 私人測試伺服器:
1. 起 daemon,`pgrep -af dollos.discord_bridge` 應見一個 bridge 子行程。
2. Discord 私訊 owner → Doll 有反應(端到端通)。
3. `kill -SIGINT <daemon_pid>` → daemon graceful 關,journal 見 bridge 乾淨收(gateway 優雅斷),`pgrep -f dollos.discord_bridge` **為空**。
4. 再起 daemon → `kill -9 <daemon_pid>` → `pgrep -f dollos.discord_bridge` **為空**(PDEATHSIG 反孤兒驗證)、無 zombie。
5. 暫時把 `bridge.toml` token 改壞 → 起 daemon → journal 應見 bridge 反覆 crash → 連續 5 次後 `giving up` + Doll 收到 `BridgeDown` perception(§9-3)。改回真 token。

---

### Task 5: dollosctl 單服務化 + legacy unit 遷移 (spec Task D / §7)

**Files:**
- Modify: `src/dollos/ctl/units.py`(刪 `render_bridge_unit`;`UnitParams` 拿掉 `bridge_config`/`daemon_ws`/`retention_days`)
- Modify: `src/dollos/ctl/cli.py`(install 拿掉 `--bridge-config` + 不寫 bridge unit + 清 legacy;uninstall 清 legacy;`logs` 拿掉 bridge choice;start/stop/restart/status 單 unit)
- Test: `tests/ctl/test_units.py` / `tests/ctl/test_cli.py`(既有;更新)

**Interfaces:**
- Consumes:daemon config 的 `[bridge].config`(bridge 路徑改由此指定,不再是 install CLI 參數)
- Produces:`dollosctl install` 只寫 `dollos-daemon.service` + 主動清除 legacy `dollos-bridge.service`;`uninstall` 同樣清 legacy(冪等)。

- [ ] **Step 1: 讀 spec §7** 全文(尤其 install+uninstall 兩條路徑都要清 legacy 的理由:防雙 bridge 撞 token)。

- [ ] **Step 2: 更新 failing tests**

`tests/ctl/test_units.py`:斷言 `render_bridge_unit` 已移除(import 應 fail 或函式不存在);`UnitParams` 無 bridge 欄位;`render_daemon_unit` 不變。
`tests/ctl/test_cli.py`:
- `install` parser 無 `--bridge-config`;install 只寫一個 unit 檔;install 會呼叫 `systemctl --user disable --now dollos-bridge.service` + `rm`(容忍失敗)。
- `uninstall` 也清 legacy bridge unit。
- `logs` 的 `which` choices 只剩 `["daemon"]`。
- `start`/`stop`/`restart` 只操作 daemon unit。

```python
def test_install_no_bridge_config_arg():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--daemon-config", "c.toml", "--bridge-config", "b.toml"])


def test_install_writes_only_daemon_unit(tmp_path, monkeypatch):
    ...  # 斷言 unit_dir 只出現 dollos-daemon.service,無 dollos-bridge.service


def test_install_cleans_legacy_bridge_unit(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_systemctl", lambda *a, **k: calls.append(a))
    ...  # 斷言有一次 disable --now dollos-bridge.service


def test_uninstall_cleans_legacy_bridge_unit(monkeypatch):
    ...  # 同上,uninstall 路徑


def test_logs_only_daemon_choice():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["logs", "bridge"])
```

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/ctl/ -v`
Expected: FAIL(現況仍寫 bridge unit / 有 --bridge-config / logs bridge)

- [ ] **Step 4: 改 `units.py`**

刪 `render_bridge_unit`(@67-87);`UnitParams`(dataclass/TypedDict)移除 `bridge_config`、`daemon_ws`、`retention_days` 欄位;`resolve_params` 對應瘦身;`render_daemon_unit` 不動。

- [ ] **Step 5: 改 `cli.py`**

- 頂部 import 拿掉 `render_bridge_unit`;`BRIDGE_UNIT = "dollos-bridge.service"` 常數保留(遷移清除要用)。
- `install()`:拿掉 `--bridge-config` 相關;只 `write_text(render_daemon_unit(params))` 一個檔;新增 legacy 清除(在寫檔前或後):

```python
    # 遷移:舊版有獨立 dollos-bridge.service。內化後 bridge 是 daemon 子行程,
    # legacy unit 若殘留且 enabled → 開機自動起 → 與內化 bridge 撞 token(spec §7)。
    # 主動 disable + rm,容忍不存在(冪等)。
    try:
        _systemctl("disable", "--now", BRIDGE_UNIT)
    except SystemctlError:
        pass
    (unit_dir / BRIDGE_UNIT).unlink(missing_ok=True)
```

- `uninstall()`:**同樣**保留上面這段 legacy 清除(否則只跑 uninstall 的使用者殘留舊 enabled unit)。
- `_build_parser()`:`install_parser` 拿掉 `--bridge-config`;`logs` 的 `which` choices 改 `["daemon"]`;`start`/`stop`/`restart`/`status` 的 help 文字改單 unit;dispatch 邏輯(`main()` @175 logs 分支等)對應改單 unit。

> 確認 `_systemctl` / `SystemctlError` 的既有名稱(grep `cli.py`),沿用其 teardown-leniency 慣例。

- [ ] **Step 6: 跑測試確認 pass**

Run: `uv run pytest tests/ctl/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/dollos/ctl/units.py src/dollos/ctl/cli.py tests/ctl/
git commit -m "refactor(ctl): single-service dollosctl (drop bridge unit, migrate legacy) (Task D)"
```

---

### Task 6: 文件收尾 (spec Task F)

**Files:**
- Modify: `CLAUDE.md`(Build/Run 段 + 架構段)、`docs/roadmap.md`、`config.example.toml`、`docs/dollosctl-smoke.md`、`bridge.example.toml`(若需註記)

**Interfaces:**(純文件)

- [ ] **Step 1: `config.example.toml`** 加 `[bridge]` 兩行範例 + 註解:

```toml
[bridge]
# 內化 discord-bridge:daemon 會把 bridge 當子行程 spawn/看顧(ServiceSupervisor)。
# enabled=false(預設)= 不起 bridge。true 需 config 指向獨立 bridge.toml(holds token)。
enabled = false
config  = "bridge.toml"
```

- [ ] **Step 2: `CLAUDE.md` Build/Run 段** 更新:
  - `dollosctl install` 不再吃 `--bridge-config`(bridge 路徑改由 daemon config `[bridge].config`)。
  - bridge log 現在在 daemon journal(`dollosctl logs daemon -f`),無 `logs bridge`。
  - 「dev 第二終端手動跑 bridge」段:註明若 daemon `[bridge].enabled=true`,**不要**再手動跑第二個 bridge(雙 bridge 撞 token)。
  - **升級註記**:既有安裝升級須**重跑一次 `dollosctl install`**(或 uninstall→install)才會清掉 legacy `dollos-bridge.service`;只做 `restart` 碰不到遷移碼。

- [ ] **Step 3: `CLAUDE.md` 架構段** 加一行:daemon 經**通用 ServiceSupervisor**(DollOS OS 級服務監督器;PDEATHSIG 反孤兒、SIGINT graceful、fatal-token 會非零退出)內化看顧 discord-bridge;bridge 仍是獨立 OS 行程(crash-isolation 零流失)。

- [ ] **Step 4: `docs/roadmap.md`** 加這步(Roadmap step:bridge 內化 + 通用 ServiceSupervisor)。

- [ ] **Step 5: `docs/dollosctl-smoke.md`** 更新單服務化後的 checklist(單 unit start/stop/restart;bridge 由 daemon 帶;升級須重跑 install;Task 4 Step 10 的 PDEATHSIG/BridgeDown 驗證併入)。

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/roadmap.md config.example.toml docs/dollosctl-smoke.md
git commit -m "docs: ServiceSupervisor bridge internalization (single-service dollosctl, migration note) (Task F)"
```

---

## Self-Review

**1. Spec coverage:**
- §3.0 ServiceSpec/_ServiceState/註冊表 → Task 2 ✓
- §3.1 argv/WS 推導(kernel 建 spec)→ Task 4 ✓
- §3.2 spawn + PDEATHSIG → Task 2 ✓
- §3.3 supervise 迴圈 + backoff + crash-loop 上限 → Task 2 ✓
- §3.4a bridge fatal 分類 → Task 3 ✓
- §3.5 graceful stop(SIGINT→SIGKILL→reap)→ Task 2 ✓
- §3.6 kernel 兩個 stop() 呼叫點 → Task 4 ✓
- §3.7 status() → Task 2 ✓
- §4 `[bridge]` config → Task 1 ✓
- §7 dollosctl 單服務化 + legacy 遷移 → Task 5 ✓
- §8 可見性/perception(BridgeDown)→ Task 4 ✓
- §9-2 pidfile(Task E)= **不做**(決策已定);§9-4 retention=模組常數 ✓
- 文件 → Task 6 ✓

**2. Placeholder scan:** Task 2/3/4 的測試骨架有 `...` 佔位 —— 這些是**斷言細節留給實作者補**(spawn OSError monkeypatch、backoff 序列、fake args/cfg/ambient),但每條測試的**目標斷言已明列**。production code 全部給完整。可接受(TDD 測試補齊是實作者的正常工作)。

**3. Type consistency:** `on_gave_up: Callable[[str, int | None], None]`(name, rc)在 spec §3.0、Task 2 實作、Task 4 `_emit_bridge_down_perception(self, name, rc)` 三處一致 ✓。`ServiceSpec.argv: tuple[str, ...]` 在 Task 2 定義、Task 4 建構一致 ✓。`_RETENTION_DAYS` 由 Task 2 定義、Task 4 import 使用 ✓。

**依序執行:** Task 1 → 2 → 3 → 4 → 5 → 6。Task 2(承重核心)與 Task 4(接線)審查用 **opus**;其餘 sonnet。Task 3 觸碰 bridge 內部,獨立好 veto。全部 merge 前跑 whole-branch **opus** review + full suite。
