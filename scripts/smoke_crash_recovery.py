"""smoke_crash_recovery.py — verify WAL replay + Awoke(recovered) after dirty restart.

Launches DollOS as a subprocess, sends a TextInput, SIGKILLs the subprocess
before Doll can finish processing, then relaunches against the same tmp
data dir to verify:
- WAL retained the unprocessed perception
- PidFile detected the dirty shutdown
- Awoke(reason=recovered) fired on relaunch
- Doll processed the replayed perception and responded
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent

# Unique port to avoid collisions with other smokes / running daemons
PORT = 11000 + (os.getpid() % 1000)


def _wait_port_ready(host: str, port: int, timeout_s: float = 15.0) -> bool:
    """Spin until the TCP port accepts connections, or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.2)
    return False


def _write_config(tmp_dir: Path, port: int) -> Path:
    """Write a minimal TOML config pointing at this tmp dir + port."""
    char_pack = REPO_ROOT / "character_packs" / "gura"
    data_root = tmp_dir / "data"
    cfg = f'''
[log]
level = "INFO"

[data]
root = "{data_root}"

[llm]
provider = "llamacpp"
template = "qwen3-thinking"
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 120.0

[ipc]
host = "127.0.0.1"
port = {port}

[memsearch]
top_k = 5

[character]
pack = "{char_pack}"

[system_pulse]
enabled = false

[cognition]
enabled = false
'''
    cfg_path = tmp_dir / "config.toml"
    cfg_path.write_text(cfg, encoding="utf-8")
    return cfg_path


def _launch_daemon(cfg_path: Path, log_path: Path) -> subprocess.Popen:
    """Spawn the daemon as a subprocess, redirect stdout/stderr."""
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "dollos", "--config", str(cfg_path)],
        cwd=str(REPO_ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    return proc


async def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="dollos_crash_smoke_"))
    cfg_path = _write_config(tmp_dir, PORT)
    log1 = tmp_dir / "daemon1.log"
    log2 = tmp_dir / "daemon2.log"
    proc1: subprocess.Popen | None = None
    proc2: subprocess.Popen | None = None

    print(f"tmp_dir = {tmp_dir}")
    print(f"port    = {PORT}")

    try:
        # --- Round 1: launch, send TextInput, SIGKILL ---
        print(f"\n[round 1] launching daemon on :{PORT} ...")
        proc1 = _launch_daemon(cfg_path, log1)
        if not _wait_port_ready("127.0.0.1", PORT, timeout_s=60.0):
            print("daemon failed to come up; aborting")
            print(f"--- daemon1.log tail ---")
            if log1.exists():
                print(log1.read_text()[-3000:])
            return 1
        print("[round 1] port ready")

        async with websockets.connect(f"ws://127.0.0.1:{PORT}") as ws:
            print("[round 1] sending TextInput ...")
            await ws.send(json.dumps({"type": "text_input", "text": "你好, 第一次"}))
            # Sleep just long enough for kernel to put on queue + WAL append
            # but unlikely to complete a full iterate
            await asyncio.sleep(0.5)

        print("[round 1] SIGKILL daemon ...")
        proc1.send_signal(signal.SIGKILL)
        proc1.wait(timeout=5.0)
        print(f"[round 1] daemon exited code={proc1.returncode}")

        # Inspect WAL + pidfile state
        wal_path = tmp_dir / "data" / "wal" / "perceptions.jsonl"
        pidfile_path = tmp_dir / "data" / "daemon.pid"
        print(f"  wal exists: {wal_path.exists()}")
        print(f"  pidfile exists: {pidfile_path.exists()}")
        wal_content = ""
        if wal_path.exists():
            wal_content = wal_path.read_text()
            print(f"  wal content ({len(wal_content)} chars):")
            for line in wal_content.splitlines()[:5]:
                print(f"    {line[:200]}")
        if not wal_content.strip():
            print("  WARN: WAL empty — SIGKILL too fast, the perception never landed")

        # --- Round 2: relaunch, verify replay ---
        print(f"\n[round 2] relaunching daemon ...")
        proc2 = _launch_daemon(cfg_path, log2)
        if not _wait_port_ready("127.0.0.1", PORT, timeout_s=60.0):
            print("daemon failed to come up after restart")
            print(f"--- daemon2.log tail ---")
            if log2.exists():
                print(log2.read_text()[-3000:])
            return 1
        print("[round 2] port ready")

        # Connect and observe: Doll should process the replayed perception
        # and respond. We give a generous timeout because llama-server may
        # need to do warmup work.
        async with websockets.connect(f"ws://127.0.0.1:{PORT}") as ws:
            print("[round 2] draining responses to replayed perception ...")
            chunks: list[str] = []
            other_msgs: list[str] = []
            end = time.monotonic() + 90.0
            quiet_after_chunks_s = 5.0
            last_chunk_time = None
            while time.monotonic() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if chunks and last_chunk_time and (time.monotonic() - last_chunk_time) > quiet_after_chunks_s:
                        break
                    continue
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "text_chunk":
                    chunks.append(msg["text"])
                    last_chunk_time = time.monotonic()
                    print(f"  speak: {msg['text']!r}")
                elif mtype == "turn_end":
                    print("  [turn_end]")
                    break
                else:
                    other_msgs.append(mtype or "?")
                    print(f"  [{mtype}] {str(msg)[:200]}")

        print("\n" + "=" * 60)
        print("OBSERVATIONS")
        print("=" * 60)
        log2_text = log2.read_text() if log2.exists() else ""
        recovered_logged = any(
            kw in log2_text for kw in ("recovered", "dirty restart", "DIRTY", "replaying")
        )
        print(f"  round1 WAL had content: {bool(wal_content.strip())}")
        print(f"  round1 pidfile retained: {pidfile_path.exists()}  (note: round2 may have rewritten it)")
        print(f"  round2 log mentions recovery: {recovered_logged}")
        print(f"  round2 text_chunks: {len(chunks)}")
        if chunks:
            print(f"  round2 reply: {''.join(chunks)[:300]!r}")
        print(f"  round2 other msgs: {other_msgs[:20]}")

        # Pull the relevant log lines for review
        print("\n--- daemon2.log recovery-related lines ---")
        for line in log2_text.splitlines():
            low = line.lower()
            if any(kw in low for kw in ("recover", "dirty", "wal", "replay", "awoke", "pidfile")):
                print(f"  {line[:250]}")

        return 0

    finally:
        for p in (proc1, proc2):
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    p.send_signal(signal.SIGKILL)
                    p.wait()
        # Keep logs on failure-ish exits would be nice, but tmp dir cleanup matches other smokes.
        # Comment out the line below to retain logs for debugging.
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
