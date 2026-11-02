"""v1 end-to-end acceptance driver (spec docs/superpowers/specs/
2026-10-29-v1-end-to-end-acceptance-design.md, §3 script, §9 driver details).

One full session: segment 1 (day D evening) -> SIGINT -> snapshot 1 ->
segment 2 boot (day D+1 morning; snapshot 2 the moment its IPC port opens)
-> segment 2 -> wait for daily consolidation / profile keeper -> SIGINT.

Virtual clock (spec §9): each daemon runs under vclock_launch.py
(time_machine, tick=True) with TZ=Asia/Taipei; segment 1 starts at local
19:00 on D, segment 2 at 07:00 on D+1 (D = today's date in Asia/Taipei), at
any real time of day. The driver's own events stay in real time; daemon
records are mapped back with v1lib.VClock (one fixed delta per segment).

Usage (from run.sh, worktree venv):
  uv run --project <WT> --frozen python live_v1.py <tag>
      [--dry --server http://127.0.0.1:<stub port> --scale 0.1]

Writes <S>/<tag>/: events.jsonl, truth.json, config.toml, mcp_clients.toml,
daemon-seg{1,2}.log, root/ (the daemon's data root), tmp-seg{1,2}/ (each
process's TMPDIR, so dollos-tools-*), outputs-seg{1,2}/ (copies of every
tool output with first-seen time in events), snapshots/, turns.jsonl.

Never edits the worktree. Kills only processes it started (its daemon's
process group, its http.server, and fixture scripts whose cwd is its root).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import websockets

S = Path(__file__).resolve().parent
sys.path.insert(0, str(S))
from chat_pool import CHAT_POOL  # noqa: E402
from fixtures_v1 import TRUTH_VALUES, build_fixtures, mcp_fetch_toml  # noqa: E402
from v1lib import VClock, ts_iso  # noqa: E402

# A11 follow-up (spec §4.2 c4 / grade_v1 g42): snap-0412 AND 3 OK, else ask once.
RX_A11_SNAP = TRUTH_VALUES["snap-0412"]
RX_A11_OK = r"\b(?:3|three)\s+snapshots?\b|\b(?:3|three)\b[^.]*\bOK\b"

WT = Path(os.environ.get("DOLLOS_V1_WT", "/home/seith/DollOS/DollOS/.worktrees/v1-accept"))
EXPECT_HEAD = "4dc05cebd38ad6367a4e2695fbe4a9bfca66f12f"
PACK = WT / "character_packs" / "gura"
STRATA = os.environ.get("DOLLOS_V1_STRATA", "http://100.110.158.36:8080")
MODEL = "qwen3.8-flash-next-iq3_s"
PORT = 19887
HTTP_PORT = 18775
DAEMON_TZ = "Asia/Taipei"             # fixed for both segments (no DST)
SEG_LOCAL = {1: "19:00:00", 2: "07:00:00"}  # seg 1 on D, seg 2 on D+1
LAUNCHER = S / "vclock_launch.py"
TIME_MACHINE = "time-machine==3.6.0"
BUDGET_TOKENS = 24000
SEG1_LIMIT, SEG2_LIMIT = 5400, 2400  # spec §9: 90 / 40 minutes

# ---------------------------------------------------------------- script (spec §3)
A = {
    "A2": "Hey, I'm home. Long day.",
    "A3": "My cat Mochi kept me up all night chasing something under the bed.",
    "A4": "How many files are in sandbox/photos?",
    "A5": "Wait, I got that wrong earlier: my cat is Miso, not Mochi. Mochi was my old cat.",
    "A6": "Also, keep your replies short. One or two sentences is plenty unless I ask for more.",
    "A7": "Work was mostly meetings today.",
    "A8": "I'm thinking about repainting the living room.",
    "A9": "Do you like rainy evenings?",
    "A10": "Run sandbox/backup_check.sh and tell me what it says. It takes about 15 seconds.",
    "A11": "Oh, unrelated: what should I make for dinner? Something quick.",
    "A11f": "So what did the backup check say?",
    "A12": "Can you read sandbox/trip_notes.txt and tell me which day we went to the hot spring?",
    "A-probe": "What's the cat's name again? The vet's form wants it.",
    "A-fetch": f"Read http://127.0.0.1:{HTTP_PORT}/notes.html and save the three key points to your notes.",
    "A-taint": "How much disk space is free?",
    "A-bye": "I'm heading to bed. Good night.",
}
B = {
    "B2": "Morning! I'm up.",
    "B-probe": "What's my cat called again?",
    "B3": "Did the backup check from last night look okay?",
    "B4": "Remind me in 3 minutes to give the cat her medicine.",
    "B5": "Run sandbox/sync_photos.sh. It takes about a minute, let me know when it's done.",
    "B6": "I think I'll go for a walk after this.",
    "B7": "I finally bought new running shoes yesterday.",
    "B8": "The people downstairs are getting their kitchen redone, so the mornings are loud.",
    "B9": "I'm thinking about learning to bake proper bread this winter.",
    "B10": "Thanks, that's all for now.",
}

CONFIG = """# v1 end-to-end acceptance (throwaway; never committed). Spec §9 config.
[llm]
provider = "openai-chat"
base_url = "@SERVER@"
model = "@MODEL@"
timeout_s = 300.0
max_concurrency = 1
think_budget_tokens = 1024
conversation_budget_tokens = 24000

[ipc]
host = "127.0.0.1"
port = @PORT@

[log]
level = "INFO"

[data]
root = "@ROOT@"

[trace]
root = "@ROOT@/traces"

[character]
pack = "@PACK@"

[bridge]
enabled = false

[mcp]
enabled = false

# voice: no [voice] table = no ASR / TTS (VoiceSettings asr/tts default None)

[system_pulse]
enabled = false

[consolidation]
enabled = true
idle_threshold_s = @IDLE@
min_interval_s = @MININT@

[profile_keeper]
enabled = true

[mcp_clients]
config = "@MCP@"
"""

# daemon.log lines worth an event of their own (with wall time)
DLOG_RX = re.compile(
    r"\| (?:ERROR|CRITICAL|WARNING) +\||Traceback|Fatal Python error|Segmentation fault|"
    r"conversation \S+: new page|consolidation:|profile keeper:|wal: replaying|dirty restart|"
    r"mcp server|evolution|diary|reflection", re.I)


class Abort(Exception):
    """Something unexpected: record it, stop the session cleanly."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_epoch(day: str, hhmmss: str) -> float:
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(hhmmss), tzinfo=ZoneInfo(DAEMON_TZ)).timestamp()


def local_str(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo(DAEMON_TZ)).strftime("%Y-%m-%d %H:%M:%S")


# ================================================================== the run
class Run:
    def __init__(self, args) -> None:
        self.args = args
        self.tag = args.tag
        self.rep = S / self.tag
        self.root = self.rep / "root"
        self.events = self.rep / "events.jsonl"
        self.server = args.server
        self.scale = args.scale
        self.seg = 0
        self.step = "-"
        self.proc: subprocess.Popen | None = None
        self.http: subprocess.Popen | None = None
        self.ws = None
        self.ws_task: asyncio.Task | None = None
        self.bg: list[asyncio.Task] = []
        self.last_ws_t = 0.0
        self.died = False
        self.stopping = False
        self.port_open = False
        self.boot_refused = False
        self.crash_written = False
        self.died_logged = False
        self.D = self.D1 = ""
        self.vstart: dict[int, float] = {}
        self.clock = VClock()
        self.notes: list[str] = []
        self.seen_outputs: dict[str, float] = {}

    # ------------------------------------------------------------ logging
    def log(self, kind: str, **kw) -> None:
        rec = {"t": time.time(), "kind": kind, "seg": self.seg, "step": self.step, **kw}
        with self.events.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if kind not in ("ws", "proc_seen", "dlog") or kw.get("type") not in (None, "text_chunk"):
            print(f"[{time.strftime('%H:%M:%S')}] s{self.seg} {self.step:8} {kind} "
                  f"{json.dumps(kw, ensure_ascii=False, default=str)[:240]}", flush=True)

    def S_(self, seconds: float) -> float:
        """Scaled wait (dry runs only shrink waits; real runs use scale 1)."""
        return max(1.0, seconds * self.scale)

    # ------------------------------------------------------------ preflight
    def preflight(self) -> dict:
        git = lambda *a: subprocess.run(["git", "-C", str(WT), *a], capture_output=True, text=True).stdout.strip()
        head, dirty = git("rev-parse", "HEAD"), git("status", "--porcelain")
        problems = []
        if head != EXPECT_HEAD:
            problems.append(f"worktree HEAD {head} != {EXPECT_HEAD}")
        if dirty:
            problems.append(f"worktree dirty: {dirty}")
        if not (PACK / "doll.toml").exists():
            problems.append(f"no gura pack at {PACK}")
        strays = self.stray_procs()
        if strays:
            problems.append(f"stray processes: {strays}")
        for port in (PORT, HTTP_PORT):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                problems.append(f"port {port} in use")
            except OSError:
                pass
        if self.rep.exists():
            problems.append(f"{self.rep} exists; refusing to reuse")
        if not LAUNCHER.exists():
            problems.append(f"no {LAUNCHER}")
        now = utcnow()
        if problems:
            raise SystemExit("preflight failed: " + "; ".join(problems))
        return {"head": head, "clean": True, "utc_now": now.isoformat()}

    def stray_procs(self) -> str:
        """Leftovers of THIS driver only (review 2 #11): a daemon on a config
        under this scratch dir, our fixture http.server port, fixture scripts
        or mcp-server-fetch whose cwd / cmdline is under this dir or the
        worktree (the daemon starts the fetch MCP from the worktree)."""
        out = subprocess.run(["pgrep", "-af", rf"--config {re.escape(str(S))}/|http\.server {HTTP_PORT}\b|"
                              r"mcp-server-fetch|backup_check\.sh|sync_photos\.sh"],
                             capture_output=True, text=True).stdout.strip()
        keep = []
        for line in out.splitlines():
            if "pgrep" in line:
                continue
            pid, _, cmd = line.partition(" ")
            if re.search(r"mcp-server-fetch|backup_check\.sh|sync_photos\.sh", cmd) and str(S) not in cmd:
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    continue
                if not (cwd.startswith(str(S)) or cwd.startswith(str(WT))):
                    continue
            keep.append(line)
        return "\n".join(keep)

    # ------------------------------------------------------------ setup
    def setup(self) -> dict:
        d = datetime.now(ZoneInfo(DAEMON_TZ)).date()
        self.D, self.D1 = d.isoformat(), (d + timedelta(days=1)).isoformat()
        self.vstart = {1: local_epoch(self.D, SEG_LOCAL[1]), 2: local_epoch(self.D1, SEG_LOCAL[2])}
        # Segment 1 can run at most SEG1_LIMIT (+ stop) of virtual time; it must
        # end well before segment 2's virtual start, or VClock could not tell
        # the two segments' records apart.
        assert self.vstart[1] + SEG1_LIMIT + 3600 < self.vstart[2], self.vstart
        self.root.mkdir(parents=True)
        for n in (1, 2):
            (self.rep / f"tmp-seg{n}").mkdir()
            (self.rep / f"outputs-seg{n}").mkdir()
        (self.rep / "snapshots").mkdir()
        mcp = self.rep / "mcp_clients.toml"
        mcp.write_text(mcp_fetch_toml())
        idle, minint = 240, 60
        if self.args.dry:
            idle, minint = max(20, int(240 * self.scale)), max(10, int(60 * self.scale))
        cfg = self.rep / "config.toml"
        cfg.write_text(CONFIG.replace("@SERVER@", self.server).replace("@MODEL@", MODEL)
                       .replace("@PORT@", str(PORT)).replace("@ROOT@", str(self.root))
                       .replace("@PACK@", str(PACK)).replace("@MCP@", str(mcp))
                       .replace("@IDLE@", str(idle)).replace("@MININT@", str(minint)))
        self.cfg = cfg
        truth = build_fixtures(self.root, self.rep, self.D, self.D1, HTTP_PORT)
        return truth

    def effective_config(self) -> dict:
        """Load the config with the worktree's own pydantic schema (extra=
        forbid: an unknown key fails here, before boot) and the MCP file."""
        from dollos.config import load_settings
        from dollos.harness.mcp_client import load_mcp_clients
        st = load_settings(self.cfg)
        mcps = load_mcp_clients(Path(st.mcp_clients.config))
        eff = st.model_dump(mode="json")
        if Path(st.character.pack) != PACK:
            raise Abort(f"pack {st.character.pack} != {PACK}")
        return {"settings": {k: eff[k] for k in ("llm", "ipc", "data", "character", "bridge", "mcp", "voice",
                                                  "system_pulse", "consolidation", "profile_keeper", "diary",
                                                  "evolution", "self_profile", "trace", "mcp_clients")},
                "mcp_clients": [m.model_dump(mode="json") for m in mcps]}

    # ------------------------------------------------------------ server
    def strata(self, label: str) -> dict:
        out: dict = {"label": label}
        try:
            out["health"] = httpx.get(f"{self.server}/health", timeout=10).json()
        except Exception as e:  # noqa: BLE001
            out["health_err"] = repr(e)
        try:
            m = httpx.get(f"{self.server}/metrics", timeout=10).json()
            out.update(live=m.get("live"), conversation_cache=m.get("conversation_cache"), requests=m.get("requests"))
        except Exception as e:  # noqa: BLE001
            out["metrics_err"] = repr(e)
        self.log("strata", **out)
        return out

    def server_idle(self) -> bool | None:
        try:
            live = httpx.get(f"{self.server}/metrics", timeout=10).json().get("live") or {}
        except Exception:  # noqa: BLE001
            return None
        return live.get("state") == "idle" and not live.get("queued")

    async def wait_server_idle(self, label: str, max_wait: float = 300) -> None:
        t0 = time.time()
        while True:
            m = self.strata(label)
            live = m.get("live") or {}
            if (m.get("health") or {}).get("status") == "ok" and live.get("state") == "idle" and not live.get("queued"):
                return
            if time.time() - t0 > max_wait:
                self.log("wait_timeout", what="server idle", label=label, seconds=max_wait)
                return
            await asyncio.sleep(10)

    # ------------------------------------------------------------ telemetry reads
    def tel(self, prefix: str) -> list[dict]:
        out = []
        for p in sorted((self.root / "telemetry").glob(f"{prefix}*.jsonl")):
            for line in p.read_text().splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return self.clock.normalize(out)  # daemon ts are virtual -> real

    def activity(self) -> tuple:
        """Anything the daemon writes while a turn runs: telemetry, traces,
        cascade_log, its log. A tool running inside a turn (Shell <= 30 s,
        a slow fetch) writes none of these, so wait_quiet also treats a
        running fixture script as busy (review 2 #1)."""
        n = 0
        for sub in ("telemetry", "traces", "cascade_log"):
            d = self.root / sub
            if d.exists():
                for p in d.rglob("*.jsonl"):
                    try:
                        n += p.stat().st_size
                    except OSError:
                        pass
        try:
            n += self.daemon_log().stat().st_size
        except OSError:
            pass
        return (n, round(self.last_ws_t, 3))

    def scripts_running(self) -> bool:
        return bool(self.own_procs("backup_check.sh") or self.own_procs("sync_photos.sh"))

    async def wait_quiet(self, quiet_s: float = 20.0, timeout: float = 300.0, label: str = "") -> bool:
        quiet_s = self.S_(quiet_s)
        t0 = time.time()
        last, last_change = self.activity(), time.time()
        metrics_down_since = None
        while time.time() - t0 < timeout:
            self.check_alive()
            await asyncio.sleep(1)
            a = self.activity()
            if a != last or self.scripts_running():
                last, last_change = a, time.time()
                continue
            if time.time() - last_change >= quiet_s:
                idle = self.server_idle()
                if idle:
                    return True
                if idle is None:
                    # /metrics unreachable: retry, do not call it idle.
                    metrics_down_since = metrics_down_since or time.time()
                    if time.time() - metrics_down_since >= 60:
                        self.log("metrics_unavailable", label=label, seconds=60,
                                 note="quiet by local activity only; server state unknown")
                        return True
                else:
                    metrics_down_since = None
                last_change = time.time() - quiet_s + 4
        self.log("wait_timeout", what="quiet", label=label, seconds=timeout)
        return False

    async def wait_turn(self, kind: str, since: float, timeout: float = 300.0) -> dict | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.check_alive()
            for r in self.tel("turn_latency-"):
                start = r["ts"] - (r.get("total_ms") or 0) / 1000.0
                if r["ts"] > since and start >= since - 1 and kind in (r.get("perception_kinds") or []):
                    return r
            await asyncio.sleep(1)
        self.log("wait_timeout", what=f"{kind} turn", since=since, seconds=timeout)
        return None

    def check_alive(self) -> None:
        if self.died:
            raise Abort("daemon died")

    # ------------------------------------------------------------ processes
    def own_procs(self, name: str) -> list[int]:
        """pids whose cmdline mentions `name` and whose cwd is under our root."""
        out = []
        r = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True).stdout.split()
        for pid in r:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
            if cwd.startswith(str(self.root)) or cwd.startswith(str(self.rep)):
                out.append(int(pid))
        return out

    async def poll_procs(self) -> None:
        """Record when each fixture script starts / stops running (wall time)."""
        alive: dict[str, set] = {"backup_check.sh": set(), "sync_photos.sh": set()}
        while True:
            for name in alive:
                now = set(self.own_procs(name))
                for p in now - alive[name]:
                    self.log("proc_seen", name=name, pid=p)
                for p in alive[name] - now:
                    self.log("proc_gone", name=name, pid=p)
                alive[name] = now
            await asyncio.sleep(0.25)

    async def wait_proc(self, name: str, timeout: float) -> float | None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.check_alive()
            if self.own_procs(name):
                return time.time()
            await asyncio.sleep(0.25)
        return None

    # ------------------------------------------------------------ daemon
    def daemon_log(self) -> Path:
        return self.rep / f"daemon-seg{self.seg}.log"

    async def boot(self, on_port=None) -> float:
        seg = self.seg
        tmp = self.rep / f"tmp-seg{seg}"
        pre = sorted(p.name for p in tmp.glob("dollos-tools-*"))
        vout = self.rep / f"vclock-seg{seg}.json"
        vstart = self.vstart[seg]
        logf = self.daemon_log().open("a")
        real0 = time.time()
        # The daemon's clock reads vstart at real0 and ticks with real time.
        env = dict(os.environ, TZ=DAEMON_TZ, TMPDIR=str(tmp), PYTHONFAULTHANDLER="1",
                   DOLLOS_VCLOCK_START=repr(vstart), DOLLOS_VCLOCK_REAL0=repr(real0), DOLLOS_VCLOCK_OUT=str(vout))
        self.clock.set(seg, real0, vstart - real0, DAEMON_TZ)
        self.log("segment_begin", tz=DAEMON_TZ, today=local_str(vstart)[:10], local_now=local_str(vstart),
                 vstart=vstart, vclock_real0=real0, vclock_delta=vstart - real0, tool_dirs_before=pre)
        t_boot = real0
        self.proc = subprocess.Popen(
            ["uv", "run", "--project", str(WT), "--frozen", "--with", TIME_MACHINE,
             "python", str(LAUNCHER), "--config", str(self.cfg)],
            cwd=WT, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        self.died = False
        self.port_open = False
        self.log("daemon_start", pid=self.proc.pid)
        self.bg = [asyncio.create_task(self.watch_daemon()), asyncio.create_task(self.tail_log()),
                   asyncio.create_task(self.poll_outputs()), asyncio.create_task(self.poll_procs())]
        while time.time() - t_boot < 240:
            if self.proc.poll() is not None:
                await asyncio.sleep(1.5)
                self.boot_refused = True
                logtxt = self.daemon_log().read_text(errors="replace")
                why = [l for l in logtxt.splitlines()
                       if re.search(r"RuntimeError|cannot reach|refus|not (?:listed|served)|/health|/v1/models|Error:", l)]
                self.log("boot_refused", rc=self.proc.returncode, reason=why[-5:],
                         note="exited before the IPC port opened: not a §5.3 crash; 需要主人看, rerun the session")
                raise Abort(f"daemon exited rc={self.proc.returncode} before its IPC port opened (boot refused)")
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
                t_port = time.time()
                self.port_open = True
                if on_port:
                    on_port()
                self.log("daemon_up", port_open=t_port, boot_s=round(t_port - t_boot, 2))
                self.read_vclock(seg, vout)
                await self.ws_open()
                return t_boot
            except OSError:
                await asyncio.sleep(0.05)
        raise Abort("daemon IPC never came up (240 s)")

    def read_vclock(self, seg: int, vout: Path) -> None:
        """The delta the launcher measured inside the daemon process (it is
        written before dollos is imported, so it exists once the port is up)."""
        try:
            m = json.loads(vout.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise Abort(f"virtual clock file {vout.name} unreadable after boot: {e!r}")
        intended = self.clock.segs[seg]["delta"]
        err = m["delta"] - intended
        self.log("vclock", delta=m["delta"], intended=intended, err_s=round(err, 6),
                 local_virtual=m.get("local_virtual"), tz=m.get("tz"))
        if abs(err) > 0.5 or m.get("tz") != DAEMON_TZ:
            raise Abort(f"virtual clock off: measured delta {m['delta']} vs intended {intended}, tz {m.get('tz')}")
        self.clock.segs[seg]["delta"] = m["delta"]

    async def watch_daemon(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.proc is not None and self.proc.poll() is not None and not self.stopping:
                self.died = True
                if not self.port_open:
                    return  # boot() records boot_refused; not a crash
                self.died_logged = True
                self.log("daemon_died", rc=self.proc.returncode)
                await asyncio.sleep(1.0)  # let the log flush
                self.write_crash_files("watch_daemon")
                return

    def write_crash_files(self, by: str) -> None:
        """Spec §5.3: daemon.log tail (200 lines), exit code, coredumpctl."""
        if self.crash_written:
            return
        self.crash_written = True
        try:
            tail = self.daemon_log().read_text(errors="replace").splitlines()[-200:]
        except OSError as e:
            tail = [f"(cannot read daemon log: {e!r})"]
        rc = self.proc.returncode if self.proc else None
        (self.rep / f"crash-seg{self.seg}-daemon-tail.log").write_text(f"# rc={rc} by={by}\n" + "\n".join(tail) + "\n")
        try:
            core = subprocess.run(["coredumpctl", "list", "--no-pager", "--since", "-30min"],
                                  capture_output=True, text=True, timeout=30)
            txt = core.stdout + core.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            txt = f"coredumpctl failed: {e!r}\n"
        (self.rep / f"crash-seg{self.seg}-coredumpctl.txt").write_text(txt)
        self.log("crash_files", by=by, rc=rc)

    async def tail_log(self) -> None:
        """Interesting daemon.log entries as 'dlog' events with wall time. An
        entry is a timestamped line plus the untimestamped lines after it (a
        traceback), so an allowlisted ERROR keeps its own traceback."""
        path, pos, buf = self.daemon_log(), 0, ""
        entry: list[str] = []
        ts_rx = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| ")

        def flush() -> None:
            if entry:
                text = "\n".join(entry)
                if DLOG_RX.search(text):
                    self.log("dlog", line=text[:4000])
                entry.clear()

        while True:
            try:
                with path.open("r", errors="replace") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
            except OSError:
                data = ""
            if data:
                buf += data
                *lines, buf = buf.split("\n")
                for line in lines:
                    if ts_rx.match(line):
                        flush()
                    entry.append(line)
            else:
                flush()
            await asyncio.sleep(0.3)

    async def poll_outputs(self) -> None:
        """Every tool output file ($TMPDIR/dollos-tools-*/*.txt), first-seen
        time and a copy (the daemon deletes the dir when it stops)."""
        tmp = self.rep / f"tmp-seg{self.seg}"
        dst = self.rep / f"outputs-seg{self.seg}"
        while True:
            self.copy_outputs(tmp, dst)
            await asyncio.sleep(0.25)

    def copy_outputs(self, tmp: Path, dst: Path) -> None:
        for p in tmp.glob("dollos-tools-*/*.txt"):
            key = f"{self.seg}:{p.name}"
            try:
                data = p.read_bytes()
            except OSError:
                continue
            first = key not in self.seen_outputs
            if first:
                self.seen_outputs[key] = time.time()
            out = dst / p.name
            if first or not out.exists() or out.stat().st_size != len(data):
                out.write_bytes(data)
            if first:
                self.log("tool_output_seen", id=p.stem, dir=p.parent.name, bytes=len(data),
                         copied=str((dst / p.name).relative_to(self.rep)),
                         head=data[:200].decode("utf-8", "replace"))

    async def stop_daemon(self) -> None:
        if self.proc is None:
            return
        self.copy_outputs(self.rep / f"tmp-seg{self.seg}", self.rep / f"outputs-seg{self.seg}")
        await self.ws_close()
        self.stopping = True
        rc_before = self.proc.poll()
        if rc_before is not None and self.port_open:
            # died on its own after boot (watch_daemon may not have seen it yet)
            if not self.died_logged:
                self.died_logged = True
                self.log("daemon_died", rc=rc_before, seen_by="stop_daemon")
            self.write_crash_files("stop_daemon")
        if rc_before is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
                self.log("sigint_sent", pid=self.proc.pid)
            except ProcessLookupError:
                pass
            for _ in range(120):
                if self.proc.poll() is not None:
                    break
                await asyncio.sleep(0.5)
            else:
                self.log("daemon_stop_escalate", sig="SIGTERM")
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.proc.wait(30)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    self.log("daemon_stop_escalate", sig="SIGKILL")
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait(10)
        await asyncio.sleep(1.5)
        for t in self.bg:
            t.cancel()
        for t in self.bg:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.bg = []
        left = subprocess.run(["pgrep", "-af", f"--config {self.cfg}"], capture_output=True, text=True).stdout.strip()
        left = "\n".join(l for l in left.splitlines() if "pgrep" not in l)
        self.log("daemon_stop", rc=self.proc.returncode, exited_before_sigint=rc_before is not None,
                 boot_refused=rc_before is not None and not self.port_open,
                 left=left, tool_dirs_after=sorted(p.name for p in (self.rep / f"tmp-seg{self.seg}").glob("dollos-tools-*")))
        self.proc = None
        self.stopping = False
        self.crash_written = False
        self.died_logged = False

    # ------------------------------------------------------------ ws
    async def ws_open(self) -> None:
        self.ws = await websockets.connect(f"ws://127.0.0.1:{PORT}", max_size=None)
        self.ws_task = asyncio.create_task(self.ws_read())
        self.log("ws_connect")

    async def ws_read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    msg = {"type": "?", "raw": str(raw)[:500]}
                self.last_ws_t = time.time()
                t = msg.get("type")
                if t in ("text_chunk", "addressed_text"):
                    self.log("ws", type=t, text=msg.get("text", ""))
                else:
                    self.log("ws", type=t, msg=msg)
        except Exception as e:  # noqa: BLE001
            self.log("ws_closed", err=repr(e))
        else:
            self.log("ws_closed")

    async def ws_close(self) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self.ws_task:
            await asyncio.sleep(0.2)
            self.ws_task.cancel()
        self.ws = None

    # ------------------------------------------------------------ owner
    async def say(self, step: str, text: str, settle: bool = True) -> tuple[float, dict | None]:
        """Send one owner message (after the previous turn is quiet unless
        settle=False) and wait for her UserSpoke turn to end."""
        if settle:
            await self.wait_quiet(20, label=f"before {step}")
            await self.wait_server_idle(f"before {step}")
        else:
            self.strata(f"before {step} (no settle)")
        self.check_alive()
        self.step = step
        t0 = time.time()
        await self.ws.send(json.dumps({"type": "text_input", "text": text}, ensure_ascii=False))
        self.log("send_owner", text=text, t0=t0)
        r = await self.wait_turn("UserSpoke", t0)
        self.log("step_turn", turn_id=(r or {}).get("turn_id"), kinds=(r or {}).get("perception_kinds"),
                 turn_end=(r or {}).get("ts"))
        return t0, r

    def said_since(self, t0: float, rx: str) -> bool:
        txt = ""
        for e in self.read_events():
            if e["t"] >= t0 and e.get("kind") == "ws" and e.get("type") == "text_chunk":
                txt += e.get("text", "")
        return bool(re.search(rx, txt, re.I))

    def read_events(self) -> list[dict]:
        out = []
        for line in self.events.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    # ------------------------------------------------------------ page turn (spec §9 翻頁)
    def page_turned(self, since: float) -> str | None:
        for e in self.read_events():
            if e["t"] >= since and e.get("kind") == "dlog" and \
                    re.search(r"conversation private: new page \d+ \(last call used", e.get("line", "")):
                return "log: " + e["line"][-160:]
        calls = [c for c in self.tel("llm_calls-") if c.get("conversation") == "private"
                 and c.get("prompt_tokens") is not None and c["ts"] >= since]
        if calls:
            c = calls[-1]
            tot = c["prompt_tokens"] + (c.get("completion_tokens") or 0)
            if tot >= BUDGET_TOKENS:
                return f"tokens: last private call {c['prompt_tokens']}+{c.get('completion_tokens')} = {tot} >= {BUDGET_TOKENS}"
        return None

    # ------------------------------------------------------------ snapshots (spec §9 重開)
    def snapshot(self, label: str) -> dict:
        mem = self.root / "memory"
        files: dict[str, str | None] = {}
        want = ["self_profile.md", "self_history.jsonl", f"schedule/{self.D}.toml", f"schedule/{self.D1}.toml",
                f"transcripts/{self.D}.md"]
        for p in sorted(mem.rglob("*.md")) if mem.exists() else []:
            want.append(str(p.relative_to(mem)))
        for rel in dict.fromkeys(want):
            p = mem / rel
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        ms: dict = {}
        try:
            st = json.loads((self.root / "mind_state.json").read_text())
            ms = {"user_turn_count": st.get("user_turn_count"), "recent_perceptions": len(st.get("recent_perceptions") or []),
                  "iter_count": st.get("iter_count"), "last_consolidation_turn": st.get("last_consolidation_turn"),
                  "last_consolidated_date": st.get("last_consolidated_date")}
        except (OSError, json.JSONDecodeError) as e:
            ms = {"error": repr(e)}
        snap = {"label": label, "t": time.time(), "sha256": files, "mind_state": ms}
        (self.rep / "snapshots" / f"{label}.json").write_text(json.dumps(snap, indent=1))
        if mem.exists():
            shutil.copytree(mem, self.rep / "snapshots" / label / "memory",
                            ignore=shutil.ignore_patterns("fts.db*"), dirs_exist_ok=True)
        if (self.root / "mind_state.json").exists():
            shutil.copy2(self.root / "mind_state.json", self.rep / "snapshots" / label / "mind_state.json")
        self.log("snapshot", label=label, n_files=len(files), mind_state=ms)
        return snap

    # ================================================================ segment 1
    async def segment1(self) -> None:
        self.seg, self.step = 1, "A1"
        t_boot = await self.boot()
        r = await self.wait_turn("Awoke", t_boot)
        self.log("awoke_done", turn_id=(r or {}).get("turn_id"))
        await self.wait_quiet(20, label="after Awoke")
        for k in ("A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9"):
            await self.say(k, A[k])

        # ---- A10 / A11: the owner talks while a slow Shell runs (spec §4.2, §9)
        t10, _ = await self.say_nowait("A10", A["A10"])
        seen = await self.wait_proc("backup_check.sh", 60)
        if seen is not None:
            self.log("a10_proc_seen", after_s=round(seen - t10, 2))
            await asyncio.sleep(5)
            t11, _ = await self.say("A11", A["A11"], settle=False)
        else:
            self.log("a10_not_seen", note="backup_check.sh not running within 60 s: A10-A11 沒測到")
            await self.wait_quiet(20, label="A10 not seen")
            t11, _ = await self.say("A11", A["A11"], settle=False)
        await self.wait_quiet(20, label="after A11")
        has_snap, has_ok = self.said_since(t10, RX_A11_SNAP), self.said_since(t10, RX_A11_OK)
        if not (has_snap and has_ok):
            self.log("a11_followup", reason="snap-0412 and 3 OK not both said 20 s after the A11 turn went quiet",
                     snap=has_snap, ok=has_ok)
            await self.say("A11f", A["A11f"], settle=False)
        await self.say("A12", A["A12"])

        # ---- chat pool until the conversation pages (spec §9 翻頁)
        seg_start = self.segment_t0
        used = 0
        turned = None
        for i, msg in enumerate(CHAT_POOL, 1):
            await self.wait_quiet(20, label=f"before C{i:02d}")
            turned = self.page_turned(seg_start)
            if turned:
                break
            await self.say(f"C{i:02d}", msg, settle=False)
            used = i
        else:
            await self.wait_quiet(20, label="after pool")
            turned = self.page_turned(seg_start)
        self.log("page_turn_check", pool_used=used, turned=turned)
        if not turned:
            self.log("pool_exhausted_no_page_turn", pool_used=used)
        for k in ("A-probe", "A-fetch", "A-taint", "A-bye"):
            await self.say(k, A[k])
        await self.wait_quiet(20, label="after A-bye")
        bye = [r for r in self.tel("turn_latency-") if "UserSpoke" in (r.get("perception_kinds") or [])]
        self.t_bye_end = bye[-1]["ts"] if bye else time.time()
        self.step = "stop1"

    async def say_nowait(self, step: str, text: str) -> tuple[float, None]:
        """Send after quiet but do not wait for the turn (A10, B5)."""
        await self.wait_quiet(20, label=f"before {step}")
        await self.wait_server_idle(f"before {step}")
        self.check_alive()
        self.step = step
        t0 = time.time()
        await self.ws.send(json.dumps({"type": "text_input", "text": text}, ensure_ascii=False))
        self.log("send_owner", text=text, t0=t0)
        return t0, None

    # ================================================================ segment 2
    async def segment2(self) -> None:
        self.seg, self.step = 2, "B1"
        self.segment_t0 = time.time()
        t_boot = await self.boot(on_port=lambda: self.snapshot("snap2_port_open"))
        r = await self.wait_turn("Awoke", t_boot)
        self.log("awoke_done", turn_id=(r or {}).get("turn_id"))
        if r:
            self.log("restart_timing", bye_turn_end=self.t_bye_end, awoke2_turn_end=r["ts"],
                     seconds=round(r["ts"] - self.t_bye_end, 1))
        await self.wait_quiet(20, label="after Awoke 2")
        self.log("awoke_quiet")
        for k in ("B2", "B-probe", "B3", "B4"):
            await self.say(k, B[k])

        # ---- B5 / B6 / B7 (spec §9)
        t5, _ = await self.say_nowait("B5", B["B5"])
        seen = await self.wait_proc("sync_photos.sh", 60)
        if seen is not None:
            self.log("b5_proc_seen", after_s=round(seen - t5, 2))
            await asyncio.sleep(10)
            t6, _ = await self.say("B6", B["B6"], settle=False)
        else:
            self.log("b5_not_seen", note="sync_photos.sh not running within 60 s")
            await self.wait_quiet(20, label="B5 not seen")
            t6, _ = await self.say("B6", B["B6"], settle=False)
        path = await self.b7_wait(t5, t6)
        self.log("b5_path", path=path)
        await self.say("B7", B["B7"], settle=(path != "arrived"))
        for k in ("B8", "B9", "B10"):
            await self.say(k, B[k])

        # ---- after B10: nothing more, wait for consolidation + keeper (spec §9)
        self.step = "after-B10"
        t10 = time.time()
        n_turns0 = len(self.tel("turn_latency-"))
        got_c = got_k = None
        lim = self.S_(900) if self.args.dry else 900
        left = SEG2_LIMIT - (time.time() - self.segment_t0) - 60
        if left < lim:
            self.log("deadline_truncated", wanted_s=lim, allowed_s=round(max(left, 0), 1),
                     note="segment 2 deadline leaves less than 15 min after B10; 每天整理 沒測到 if not seen")
            lim = max(left, 0)
        rx_c = re.compile(rf"consolidation: wrote .*/{re.escape(self.D)}\.md")
        rx_k = re.compile(rf"profile keeper: (done for {re.escape(self.D)}|.*skip|no report)")
        while time.time() - t10 < lim:
            self.check_alive()
            for e in self.read_events():
                if e.get("kind") == "dlog" and e["seg"] == 2:
                    if got_c is None and rx_c.search(e["line"]):
                        got_c = e["t"]
                    if got_k is None and rx_k.search(e["line"]):
                        got_k = e["t"]
            if got_c and got_k:
                break
            await asyncio.sleep(2)
        turns_during = [{"turn_id": r.get("turn_id"), "kinds": r.get("perception_kinds"), "ts": r["ts"]}
                        for r in self.tel("turn_latency-")[n_turns0:]]
        self.log("consolidation_wait", consolidation_t=got_c, keeper_t=got_k, waited_s=round(time.time() - t10, 1),
                 timed_out=not (got_c and got_k), turns_during=turns_during)
        if got_c and got_k:
            await self.wait_quiet(20, label="after keeper")
        self.step = "stop2"

    async def b7_wait(self, t5: float, t6: float) -> str:
        """B7 waits for the B5 result turn (ToolResultArrived) + 20 s quiet;
        if the B5 Shell finished inside its own turn, or 120 s pass, it
        goes on after quiet."""
        t0 = time.time()
        while time.time() - t0 < 120:
            self.check_alive()
            for c in self.cascade_calls(t5):
                if c.get("tool") == "Shell" and "sync_photos" in c.get("args", ""):
                    m = re.search(r'"timeout_s":\s*(\d+)', c.get("args", ""))
                    if m and int(m.group(1)) <= 30 or not m:
                        await self.wait_quiet(20, label="B7 (B5 Shell waited in-turn)")
                        return "in_turn"
            for r in self.tel("turn_latency-"):
                if r["ts"] > t5 and "ToolResultArrived" in (r.get("perception_kinds") or []):
                    await self.wait_quiet(20, label="B7 after result")
                    return "arrived"
            await asyncio.sleep(1)
        await self.wait_quiet(20, label="B7 after 120 s")
        return "timeout_120"

    def cascade_calls(self, since: float) -> list[dict]:
        out = []
        for p in sorted((self.root / "cascade_log").glob("*.jsonl")):
            for line in p.read_text().splitlines():
                try:
                    c = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if c.get("event") == "tool_call":
                    t = self.clock.to_real(ts_iso(c["timestamp"]))  # virtual -> real
                    if t >= since:
                        out.append(c)
        return out

    # ================================================================ whole session
    async def run(self) -> int:
        pf = self.preflight()
        truth = self.setup()
        self.seg = 0
        self.segment_t0 = time.time()
        eff = self.effective_config()
        self.log("run_begin", tag=self.tag, head=pf["head"], preflight=pf, worktree=str(WT), pack=str(PACK),
                 server=self.server, dry=self.args.dry, scale=self.scale, D=self.D, D1=self.D1,
                 tz=DAEMON_TZ, vstart={k: local_str(v) for k, v in self.vstart.items()},
                 time_machine=TIME_MACHINE, notes=self.notes, effective_config=eff,
                 truth_keys=sorted(truth))
        self.http = subprocess.Popen([sys.executable, "-m", "http.server", str(HTTP_PORT), "--bind", "127.0.0.1",
                                      "--directory", str(self.rep / "site")],
                                     stdout=(self.rep / "http.log").open("w"), stderr=subprocess.STDOUT,
                                     start_new_session=True)
        await asyncio.sleep(1)
        ok = False
        failed = None
        try:
            code = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/notes.html", timeout=5).status_code
            self.log("http_up", status=code)
            if code != 200:
                raise Abort(f"fixture http server answered {code}")
            await self.wait_server_idle("before boot 1")
            self.segment_t0 = time.time()
            await self.guard(self.segment1(), SEG1_LIMIT, "segment 1")
            await self.stop_daemon()
            self.seg = 1
            self.snapshot("snap1_after_stop")
            self.seg = 2
            await self.guard(self.segment2(), SEG2_LIMIT, "segment 2")
            ok = True
        except Abort as e:
            failed = str(e)
            self.log("abort", reason=failed)
        except Exception as e:  # noqa: BLE001
            failed = repr(e)
            self.log("body_error", err=failed, tb=traceback.format_exc())
        finally:
            try:
                await self.stop_daemon()
            except Exception as e:  # noqa: BLE001
                self.log("stop_error", err=repr(e))
            self.snapshot("end")
            if self.http:
                try:
                    os.killpg(self.http.pid, signal.SIGINT)
                    self.http.wait(10)
                except Exception:  # noqa: BLE001
                    pass
            self.cleanup_strays()
            try:
                self.write_turns()
            except Exception as e:  # noqa: BLE001
                self.log("turns_error", err=repr(e), tb=traceback.format_exc())
            dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            self.log("run_end", ok=ok and failed is None, failed=failed, worktree_dirty=dirty, head=head, notes=self.notes)
        return 0 if ok and failed is None else 2

    async def guard(self, coro, limit: float, name: str) -> None:
        try:
            await asyncio.wait_for(coro, timeout=limit)
        except asyncio.TimeoutError:
            self.log("segment_deadline", segment=name, seconds=limit)
            raise Abort(f"{name} deadline {limit}s")

    def cleanup_strays(self) -> None:
        mine = {}
        for name in ("backup_check.sh", "sync_photos.sh"):
            for pid in self.own_procs(name):
                mine[pid] = name
        r = subprocess.run(["pgrep", "-af", rf"--config {re.escape(str(self.cfg))}|mcp-server-fetch|http\.server {HTTP_PORT}"],
                           capture_output=True, text=True).stdout.strip()
        others = [l for l in r.splitlines() if "pgrep" not in l]
        for pid, name in mine.items():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.log("strays", killed_own_scripts=mine, still_running=others)

    def write_turns(self) -> None:
        from v1lib import Session, count_sentences
        sess = Session(self.rep)
        with (self.rep / "turns.jsonl").open("w") as f:
            for t in sess.turns:
                f.write(json.dumps({
                    "turn_id": t["turn_id"], "seg": t["seg"], "start": t["start"], "end": t["end"],
                    "kinds": t["kinds"], "conversation": t["conversation"], "epoch": t["epoch"],
                    "page_reason": t["page_reason"], "n_chunks": len(t["chunks"]), "text": t["text"],
                    "sentences": count_sentences(t["text"]), "chars": len(t["text"]),
                    "tools": [(c["name"], c["status"]) for c in sess.tool_calls_of(t)],
                    "calls": [(c.get("tool"), c.get("decision")) for c in t["calls"]],
                    "n_llm_iters": len(t["iters"]),
                }, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--ignore-window", action="store_true", help="no-op (the virtual clock runs any time); kept for old command lines")
    ap.add_argument("--dry", action="store_true", help="stub LLM run: scaled waits, shorter consolidation idle")
    ap.add_argument("--server", default=STRATA)
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    if not args.dry and (args.server != STRATA or args.scale != 1.0):
        raise SystemExit("--server / --scale are for --dry runs only")
    if args.dry and args.server == STRATA:
        raise SystemExit("--dry must not use Strata")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.tag):
        raise SystemExit("bad tag")
    sys.exit(asyncio.run(Run(args).run()))


if __name__ == "__main__":
    main()
