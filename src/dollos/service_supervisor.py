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
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

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


def _make_preexec(parent_pid: int) -> Callable[[], None]:
    """Build the child's pre-exec hook, closing over the daemon's pid as
    captured in the PARENT before spawning.

    `os.getppid() == 1` alone is not a reliable orphan check: under
    `systemd --user` the daemon runs inside a user-session scope that acts
    as a subreaper (`PR_SET_CHILD_SUBREAPER`), so an orphaned child
    reparents to systemd's pid, never to 1. Comparing against the actual
    parent pid detects reparenting to init OR to any subreaper.
    """
    def _preexec() -> None:
        """child 內、exec 之前跑(Linux-only)。反孤兒 + 獨立 process group。"""
        os.setsid()                              # 獨立 group(killpg 能連孫行程一起收)
        _prctl_pdeathsig(signal.SIGINT)          # daemon 一死 → kernel 對本行程發 SIGINT
        if os.getppid() != parent_pid:            # race guard:prctl 前 daemon 已死(或被 subreaper 收養)
            os._exit(0)                          # PDEATHSIG 已錯過 → 自盡防孤兒
    return _preexec


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
        parent_pid = os.getpid()                 # 一定要在 spawn 前、parent 行程裡取
        proc = await asyncio.create_subprocess_exec(
            *st.spec.argv,
            stdout=None, stderr=None,            # 繼承 daemon fds → daemon journal
            preexec_fn=_make_preexec(parent_pid),
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
            except TimeoutError:
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
