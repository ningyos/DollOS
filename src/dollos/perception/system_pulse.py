"""SystemPulse — Doll's proprioception of her host.

The computer IS Doll's body. CPU load, memory, GPU heat, battery, focus
window etc. are her vital signs. This module polls them every N seconds
in the background, buckets each into coarse categories, and only emits
text when a bucket *changes* — so the LLM sees a `[Self pulse]` block
only when the body actually feels different, not on every iteration.

Output is a multi-line first-person body-language block consumed by
``render_mind`` and appended after ``[Active monitors]``. Missing
sources (no DISPLAY, no battery on desktop, no nvidia-smi, etc.) are
silently omitted — no fake values, no fallbacks.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# Bucketing helpers ----------------------------------------------------

def bucket_load(load1: float, ncpu: int) -> str:
    if ncpu <= 0:
        ncpu = 1
    ratio = load1 / ncpu
    if ratio < 0.4:
        return "light"
    if ratio < 1.0:
        return "steady"
    return "heavy"


def bucket_mem(pct: float) -> str:
    if pct < 60.0:
        return "comfortable"
    if pct < 85.0:
        return "tight"
    return "pressured"


def bucket_gpu_temp(temp_c: float) -> str:
    if temp_c < 55.0:
        return "cool"
    if temp_c < 75.0:
        return "warm"
    return "hot"


def bucket_battery(pct: float) -> str:
    if pct >= 95.0:
        return "full"
    if pct >= 30.0:
        return "healthy"
    if pct >= 15.0:
        return "low"
    return "critical"


def bucket_idle(idle_s: float) -> str:
    if idle_s < 60.0:
        return "present"
    if idle_s < 600.0:
        return "away"
    return "long-away"


def thermal_multiplier(temp_c: float, warm: float, hot: float) -> float:
    """Metabolic vital model §2.3 (Task 5): a hot GPU makes the SAME work cost
    more ATP. Pure function, reuses ``bucket_gpu_temp``'s existing 55/75°C
    buckets so the drain multiplier and the `[Self pulse]` heat wording never
    drift apart. cool -> 1.0 (no penalty), warm -> ``warm``, hot -> ``hot``."""
    b = bucket_gpu_temp(temp_c)
    return hot if b == "hot" else warm if b == "warm" else 1.0


# Sample dataclass -----------------------------------------------------

@dataclass
class PulseSample:
    taken_at: datetime
    load1: float | None = None
    ncpu: int | None = None
    mem_used_pct: float | None = None
    gpus: list[tuple[float, float]] = field(default_factory=list)
    gpu_power: list[tuple[float, float]] = field(default_factory=list)
    battery_pct: float | None = None
    battery_status: str | None = None
    active_window: str | None = None
    idle_s: float | None = None
    network_open: bool | None = None

    def signature(self) -> tuple:
        load_b = bucket_load(self.load1, self.ncpu or 1) if self.load1 is not None else None
        mem_b = bucket_mem(self.mem_used_pct) if self.mem_used_pct is not None else None
        gpu_b = tuple(
            (round(m / 10) * 10, bucket_gpu_temp(t)) for m, t in self.gpus
        ) if self.gpus else ()
        batt_b = bucket_battery(self.battery_pct) if self.battery_pct is not None else None
        idle_b = bucket_idle(self.idle_s) if self.idle_s is not None else None
        return (
            load_b,
            mem_b,
            gpu_b,
            batt_b,
            self.battery_status,
            self.active_window,
            idle_b,
            self.network_open,
        )


# Source readers — return None if unavailable -------------------------

def read_loadavg() -> tuple[float | None, int | None]:
    try:
        text = Path("/proc/loadavg").read_text()
        load1 = float(text.split()[0])
        return load1, os.cpu_count() or 1
    except Exception:
        return None, None


def read_meminfo() -> float | None:
    try:
        text = Path("/proc/meminfo").read_text()
        fields: dict[str, int] = {}
        for line in text.splitlines():
            m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
            if m:
                fields[m.group(1)] = int(m.group(2))
        total = fields.get("MemTotal")
        avail = fields.get("MemAvailable")
        if total is None or avail is None or total <= 0:
            return None
        return ((total - avail) / total) * 100.0
    except Exception:
        return None


async def _run_cmd(argv: list[str], timeout_s: float) -> str | None:
    """Run a subprocess via execve (no shell). Returns stdout or None."""
    if shutil.which(argv[0]) is None:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", errors="replace")
    except Exception:
        return None
    finally:
        # CancelledError (BaseException) bypasses except Exception; ensure the
        # process is killed so it doesn't linger after task cancellation.
        if proc is not None and proc.returncode is None:
            proc.kill()


async def read_nvidia_smi(
    timeout_s: float = 2.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Returns (gpus, gpu_power).

    ``gpus`` is (mem_used_pct, temp_c) per GPU, as before. ``gpu_power`` is
    (draw_w, limit_w) per GPU that reported power telemetry — parsed
    independently of mem/temp so a card (or old driver) missing power
    columns still contributes its mem/temp reading; it just doesn't
    contribute a power.draw entry.
    """
    text = await _run_cmd(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ],
        timeout_s,
    )
    if text is None:
        return [], []
    gpus: list[tuple[float, float]] = []
    gpu_power: list[tuple[float, float]] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            used = float(parts[0])
            total = float(parts[1])
            temp = float(parts[2])
        except ValueError:
            continue
        if total <= 0:
            continue
        gpus.append(((used / total) * 100.0, temp))
        draw = lim = None
        if len(parts) >= 5:
            try:
                draw, lim = float(parts[3]), float(parts[4])
            except ValueError:
                draw = lim = None
        if draw is not None and lim is not None:
            gpu_power.append((draw, lim))
    return gpus, gpu_power


def read_battery() -> tuple[float | None, str | None]:
    base = Path("/sys/class/power_supply")
    if not base.exists():
        return None, None
    try:
        bats = sorted(p for p in base.iterdir() if p.name.startswith("BAT"))
    except Exception:
        return None, None
    if not bats:
        return None, None
    try:
        cap = float((bats[0] / "capacity").read_text().strip())
        status = (bats[0] / "status").read_text().strip()
        return cap, status
    except Exception:
        return None, None


async def read_active_window(timeout_s: float = 1.0) -> str | None:
    if not os.environ.get("DISPLAY"):
        return None
    text = await _run_cmd(
        ["xdotool", "getactivewindow", "getwindowname"], timeout_s
    )
    if text is None:
        return None
    title = text.strip()
    return title or None


async def read_idle_seconds(timeout_s: float = 1.0) -> float | None:
    if not os.environ.get("DISPLAY"):
        return None
    text = await _run_cmd(["xprintidle"], timeout_s)
    if text is None:
        return None
    try:
        return float(text.strip()) / 1000.0
    except ValueError:
        return None


async def check_network(host: str = "1.1.1.1", port: int = 53, timeout_s: float = 1.0) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout_s)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (TimeoutError, OSError, socket.gaierror):
        return False
    except Exception:
        return False


# Block rendering — first-person body language ------------------------

def render_block(sample: PulseSample, *, include_active_window: bool) -> str:
    ts = sample.taken_at.strftime("%H:%M:%S")
    lines: list[str] = [f"[Self pulse {ts}]"]

    if sample.load1 is not None and sample.ncpu is not None:
        b = bucket_load(sample.load1, sample.ncpu)
        lines.append(f"- thought load: {b} ({sample.load1:.2f} on {sample.ncpu} cores)")

    if sample.mem_used_pct is not None:
        b = bucket_mem(sample.mem_used_pct)
        lines.append(f"- memory: {b} ({sample.mem_used_pct:.0f}%)")

    if sample.gpus:
        mem_parts = " / ".join(f"{m:.0f}%" for m, _ in sample.gpus)
        temp_parts = " / ".join(f"{t:.0f}°C" for _, t in sample.gpus)
        hottest = max(t for _, t in sample.gpus)
        b = bucket_gpu_temp(hottest)
        line = f"- vital heat: {b} ({temp_parts}); GPU mem {mem_parts}"
        if sample.gpu_power:
            max_draw = max(draw for draw, _ in sample.gpu_power)
            line += f" · {max_draw:.0f}w"
        lines.append(line)

    if sample.battery_pct is not None:
        b = bucket_battery(sample.battery_pct)
        status = (sample.battery_status or "?").lower()
        if status == "charging":
            tail = "plugged in"
        elif status == "full":
            tail = "full, plugged in"
        elif status == "discharging":
            tail = "on battery"
        else:
            tail = sample.battery_status or "?"
        lines.append(f"- charge: {b}, {sample.battery_pct:.0f}% ({tail})")

    if include_active_window and sample.active_window is not None:
        lines.append(f"- attention: {sample.active_window}")

    if sample.idle_s is not None:
        b = bucket_idle(sample.idle_s)
        if sample.idle_s < 60:
            human = f"{sample.idle_s:.0f}s"
        elif sample.idle_s < 3600:
            human = f"{sample.idle_s / 60:.0f}m"
        else:
            human = f"{sample.idle_s / 3600:.1f}h"
        lines.append(f"- breath since last input: {b} ({human} ago)")

    if sample.network_open is not None:
        lines.append(f"- outside link: {'open' if sample.network_open else 'silent'}")

    return "\n".join(lines)


# Worker ---------------------------------------------------------------

async def _none() -> None:
    return None


class SystemPulse:
    """Background poller + snapshot provider.

    Lifecycle mirrors MonitorRunner: ``start()`` kicks off the polling
    task; ``stop()`` cancels it; ``snapshot()`` returns either a freshly
    rendered block (when the body has shifted bucket since the last
    emit) or None.
    """

    def __init__(
        self,
        *,
        poll_interval_s: float = 60.0,
        include_active_window: bool = True,
        enabled: bool = True,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._include_active_window = include_active_window
        self._enabled = enabled
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_sample: PulseSample | None = None
        self._last_emitted_sig: tuple | None = None

    def start(self) -> None:
        if not self._enabled:
            logger.info("SystemPulse disabled by config")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="system-pulse")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                self._last_sample = await self._poll_once()
            except Exception:
                logger.exception("system_pulse poll failed; continuing")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> PulseSample:
        load1, ncpu = read_loadavg()
        mem_pct = read_meminfo()
        battery_pct, battery_status = read_battery()
        nvidia_result, active_window, idle_s, net_open = await asyncio.gather(
            read_nvidia_smi(),
            read_active_window() if self._include_active_window else _none(),
            read_idle_seconds(),
            check_network(),
        )
        gpus, gpu_power = nvidia_result
        return PulseSample(
            taken_at=datetime.now(),
            load1=load1,
            ncpu=ncpu,
            mem_used_pct=mem_pct,
            gpus=gpus,
            gpu_power=gpu_power,
            battery_pct=battery_pct,
            battery_status=battery_status,
            active_window=active_window,
            idle_s=idle_s,
            network_open=net_open,
        )

    def snapshot(self) -> str | None:
        if not self._enabled:
            return None
        sample = self._last_sample
        if sample is None:
            return None
        sig = sample.signature()
        if sig == self._last_emitted_sig:
            return None
        self._last_emitted_sig = sig
        return render_block(sample, include_active_window=self._include_active_window)

    def latest_idle_s(self) -> float | None:
        """Idle seconds from the last fresh pulse sample, else None.

        None when: disabled / no sample yet / idle source unavailable /
        sample is stale (older than 2x poll interval).
        """
        if not self._enabled:
            return None
        s = self._last_sample
        if s is None or s.idle_s is None:
            return None
        age = (datetime.now() - s.taken_at).total_seconds()
        if age > 2 * self._poll_interval_s:
            return None
        return s.idle_s

    def latest_sample(self) -> PulseSample | None:
        """The last fresh PulseSample, else None.

        None when: disabled / no sample yet / sample is stale (older than 2x
        poll interval). Mirrors ``latest_idle_s``'s staleness guard so a dead
        poll loop can't feed the PulseObserver an ancient reading.
        """
        if not self._enabled:
            return None
        s = self._last_sample
        if s is None:
            return None
        age = (datetime.now() - s.taken_at).total_seconds()
        if age > 2 * self._poll_interval_s:
            return None
        return s

    async def poll_now(self) -> None:
        """Force one immediate poll (for tests / smoke)."""
        self._last_sample = await self._poll_once()
