import asyncio
import os
import sys
import time

import pytest

import dollos.service_supervisor as service_supervisor
from dollos.service_supervisor import ServiceSpec, ServiceSupervisor


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
    assert sup.status()[0]["pid"] is None
    await sup.stop()


@pytest.mark.asyncio
async def test_crash_then_backoff_restart(monkeypatch):
    # 讓 backoff 常數變小以加速;直接斷言 consecutive_failures 遞增且處於 restarting。
    monkeypatch.setattr(service_supervisor, "_BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(service_supervisor, "_MAX_CONSECUTIVE", 1000)
    monkeypatch.setattr(service_supervisor, "_HEALTHY_UPTIME_S", 9999.0)

    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(CRASH_EXIT)))
    sup.start()

    # 等到至少崩潰重啟兩次
    for _ in range(100):
        if sup.status()[0]["consecutive_failures"] >= 2:
            break
        await asyncio.sleep(0.05)

    status = sup.status()[0]
    assert status["consecutive_failures"] >= 2
    assert status["phase"] == "restarting"
    await sup.stop()


@pytest.mark.asyncio
async def test_crash_loop_gives_up_and_calls_on_gave_up(monkeypatch):
    # 用 monkeypatch 把 _BACKOFF_BASE_S / _MAX_CONSECUTIVE 調小加速
    monkeypatch.setattr(service_supervisor, "_BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(service_supervisor, "_MAX_CONSECUTIVE", 3)
    monkeypatch.setattr(service_supervisor, "_HEALTHY_UPTIME_S", 9999.0)

    calls = []
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(CRASH_EXIT),
                             on_gave_up=lambda name, rc: calls.append((name, rc))))
    sup.start()
    # 等到超過 _MAX_CONSECUTIVE
    for _ in range(100):
        if sup.status()[0]["phase"] == "gave_up":
            break
        await asyncio.sleep(0.05)
    assert calls and calls[0][0] == "t"
    assert calls[0][1] == 3  # CRASH_EXIT rc
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
async def test_sigint_ignored_escalates_to_sigkill(monkeypatch):
    # GRACE 常數 monkeypatch 調小;IGNORE_SIGINT script → 逾時 → SIGKILL → reap
    monkeypatch.setattr(service_supervisor, "_GRACE_S", 0.3)

    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(IGNORE_SIGINT)))
    sup.start()
    await asyncio.sleep(0.3)
    pid = sup.status()[0]["pid"]
    assert pid is not None

    start = time.monotonic()
    await sup.stop()
    elapsed = time.monotonic() - start

    # 逃過 SIGINT → 一定要撐過 grace 才被 SIGKILL 收掉(不是立刻死)
    assert elapsed >= 0.3
    assert not _pid_alive(pid)
    assert sup.status()[0]["phase"] == "stopped"


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
    sup.start()
    sup.start()   # 第二次是 no-op(每服務 idempotency guard)
    await asyncio.sleep(0.2)
    # 只有一個 supervise task 在跑(不是兩個 loop 兩個 proc)
    tasks = [t for t in asyncio.all_tasks() if t.get_name() == "svc:t"]
    assert len(tasks) == 1
    assert sup.status()[0]["pid"] is not None
    await sup.stop()


@pytest.mark.asyncio
async def test_spawn_oserror_treated_as_crash(monkeypatch):
    # monkeypatch asyncio.create_subprocess_exec 丟 OSError → supervise 迴圈當一次 crash,
    # 不讓 task 靜默死掉(斷言 consecutive_failures 遞增 / 最終 gave_up)。
    monkeypatch.setattr(service_supervisor, "_BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(service_supervisor, "_MAX_CONSECUTIVE", 2)
    monkeypatch.setattr(service_supervisor, "_HEALTHY_UPTIME_S", 9999.0)

    async def _boom(*args, **kwargs):
        raise OSError("EAGAIN (simulated resource exhaustion)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    calls = []
    sup = ServiceSupervisor()
    sup.register(ServiceSpec(name="t", argv=_argv(SLEEP_FOREVER),
                             on_gave_up=lambda name, rc: calls.append((name, rc))))
    sup.start()

    for _ in range(100):
        if sup.status()[0]["phase"] == "gave_up":
            break
        await asyncio.sleep(0.05)

    assert sup.status()[0]["phase"] == "gave_up"
    assert sup.status()[0]["consecutive_failures"] > 2
    assert calls and calls[0][0] == "t"
    assert calls[0][1] is None  # spawn 例外沒有 rc
    await sup.stop()


@pytest.mark.asyncio
async def test_pdeathsig_kills_child_when_parent_dies(tmp_path):
    # 假父:一個 python 子行程,它用 ServiceSupervisor spawn 一個 sleeper,印出
    # sleeper pid,然後自己 sleep。我們 SIGKILL 假父 → PDEATHSIG(SIGINT)應讓
    # sleeper 隨之消失。
    sleeper_argv = "sys.executable,'-c','import time\\nwhile True: time.sleep(3600)'"
    fake_parent = (
        "import asyncio, sys\n"
        "from dollos.service_supervisor import ServiceSupervisor, ServiceSpec\n"
        "async def main():\n"
        "    sup = ServiceSupervisor()\n"
        f"    sup.register(ServiceSpec(name='s', argv=({sleeper_argv})))\n"
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
