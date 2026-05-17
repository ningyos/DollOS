#!/usr/bin/env python
"""smoke_mind_basic.py — MindLoop sanity smoke.

Spins up DollOS in-process, sends "你好", waits for at least one TextChunk
(Say) within 120s. Validates MindLoop basic lifecycle + IPC.

Pass: receives at least one text_chunk.
Fail: timeout with no text_chunk, or crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from datetime import date as _date
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dollos.config import (  # noqa: E402
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS  # noqa: E402

LOG_PATH = Path("/tmp/smoke_mind_basic.log")
TIMEOUT_S = 120.0


def _setup_logging() -> None:
    LOG_PATH.write_text("")
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)


def _build_settings(tmp_root: Path) -> Settings:
    pack = REPO_ROOT / "character_packs" / "gura"
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp_root / "data"),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack),
    )


async def _collect_say(ws, timeout_s: float) -> tuple[str, bool]:
    """Collect text_chunk messages until silence for 8s (no more chunks).
    Returns (accumulated_text, got_any).
    """
    chunks: list[str] = []
    got_any = False
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Wait for next chunk with 8s idle-silence cutoff after first chunk
        wait = min(remaining, 8.0 if got_any else remaining)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            if got_any:
                break  # silence after speaking = done
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "text_chunk":
            chunks.append(msg.get("text", ""))
            got_any = True
    return "".join(chunks), got_any


async def _run() -> int:
    _setup_logging()
    t0 = time.monotonic()
    print(f"[smoke_basic] log → {LOG_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-basic-"))
    print(f"[smoke_basic] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    # Suppress daily bootstrap (avoid WriteSchedule noise)
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()

    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    # Push Awoke
    import time as _time
    from dollos.mind.mind_state import Perception
    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "cold_start"})
    )

    result_text = ""
    got_reply = False
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke_basic] WS → {uri}")

        async with websockets.connect(uri) as ws:
            print('[smoke_basic] sending: "你好"')
            await ws.send(json.dumps({"type": "text_input", "text": "你好"}))
            result_text, got_reply = await _collect_say(ws, timeout_s=TIMEOUT_S)
    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    wall = time.monotonic() - t0

    print("\n========== smoke_mind_basic RESULT ==========")
    if got_reply:
        print(f"PASS — received Say in {wall:.1f}s")
    else:
        print(f"FAIL — no Say within {TIMEOUT_S}s (wall={wall:.1f}s)")
    print(f"Doll said: {result_text[:300]!r}")
    print(f"Wall time: {wall:.1f}s")
    print(f"MindLoop iter count: {dollos._mind_state.iter_count}")
    print(f"Recent outputs: {list(dollos._mind_state.recent_outputs)}")
    print(f"Log: {LOG_PATH}")

    return 0 if got_reply else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
