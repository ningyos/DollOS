#!/usr/bin/env python
"""smoke_mind_persistence.py — MindState persistence + Recall across restarts.

Phase 1: Start daemon, send "記下我喜歡 25°C 冷氣" → wait for NoteMemory.
Phase 2: Kill daemon (shutdown), restart fresh daemon pointing at same data/.
Phase 3: Send "我喜歡幾度冷氣?" → verify Doll recalls "25°C".

Pass: Doll mentions "25" in response after restart.
Partial: responds but doesn't mention 25.
Fail: crash or no response.
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

LOG_PATH = Path("/tmp/smoke_mind_persistence.log")
TIMEOUT_S = 90.0
PROMPT_1 = "記下我喜歡 25°C 冷氣"
PROMPT_2 = "我喜歡幾度冷氣?"


def _setup_logging() -> None:
    LOG_PATH.write_text("")
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)


def _build_settings(data_root: Path) -> Settings:
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
        data=DataConfig(root=data_root),
        memsearch=MemsearchConfig(top_k=10),
        character=CharacterConfig(pack=pack),
    )


async def _collect_say(ws, timeout_s: float, silence_s: float = 10.0) -> tuple[str, bool]:
    chunks: list[str] = []
    got_any = False
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait = min(remaining, silence_s if got_any else remaining)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            if got_any:
                break
            continue
        msg = json.loads(raw)
        if msg.get("type") == "text_chunk":
            chunks.append(msg.get("text", ""))
            got_any = True
    return "".join(chunks), got_any


async def _run_phase1(data_root: Path) -> tuple[str, bool, int]:
    """Phase 1: start daemon, note memory, return (say_text, noted, iter_count)."""
    import time as _time
    from dollos.mind.mind_state import Perception

    settings = _build_settings(data_root)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()
    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "cold_start"})
    )
    # Let Awoke settle
    await asyncio.sleep(12.0)

    say_text = ""
    got_reply = False
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[phase1] WS → {uri}")
        print(f"[phase1] sending: {PROMPT_1!r}")

        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT_1}))
            say_text, got_reply = await _collect_say(ws, timeout_s=TIMEOUT_S)
            print(f"[phase1] reply: {say_text[:150]!r}")

        # Wait for any NoteMemory to finish writing
        await asyncio.sleep(3.0)
    finally:
        iter_count = dollos._mind_state.iter_count
        outputs = list(dollos._mind_state.recent_outputs)
        noted = any(o.kind == "NoteMemory" for o in outputs)
        print(f"[phase1] NoteMemory called: {noted}")
        print(f"[phase1] iter_count: {iter_count}")
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    return say_text, noted, iter_count


async def _run_phase2(data_root: Path) -> tuple[str, bool]:
    """Phase 2: restart daemon on same data_root, ask about preference."""
    import time as _time
    from dollos.mind.mind_state import Perception

    settings = _build_settings(data_root)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(_date.today())

    print(f"[phase2] restarted daemon with data_root={data_root}")
    print(f"[phase2] mind_state.iter_count from disk: {dollos._mind_state.iter_count}")

    await dollos.memsearch.index()
    await dollos.server.start()
    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "resumed"})
    )
    # Let Awoke settle
    await asyncio.sleep(12.0)

    say_text = ""
    got_reply = False
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[phase2] WS → {uri}")
        print(f"[phase2] sending: {PROMPT_2!r}")

        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT_2}))
            say_text, got_reply = await _collect_say(ws, timeout_s=TIMEOUT_S)
            print(f"[phase2] reply: {say_text[:200]!r}")
    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    return say_text, got_reply


async def _run() -> int:
    _setup_logging()
    t0 = time.monotonic()
    print(f"[smoke_persistence] log → {LOG_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-persist-"))
    data_root = tmp_root / "data"
    print(f"[smoke_persistence] data root → {data_root}")

    # Phase 1
    print("\n--- PHASE 1: note memory ---")
    p1_say, p1_noted, p1_iters = await _run_phase1(data_root)

    # Check memory file was written
    memory_files = list((data_root / "memory" / "shared").glob("*.md")) if (
        data_root / "memory" / "shared"
    ).exists() else []
    print(f"[smoke_persistence] memory files after phase1: {[f.name for f in memory_files]}")
    if memory_files:
        print(f"[smoke_persistence] content: {memory_files[0].read_text()[:300]!r}")

    # Phase 2 (restart)
    print("\n--- PHASE 2: restart and recall ---")
    p2_say, p2_got = await _run_phase2(data_root)

    wall = time.monotonic() - t0
    recalls_25 = "25" in p2_say

    print("\n========== smoke_mind_persistence RESULT ==========")
    if p2_got and recalls_25:
        verdict = "PASS — Doll recalled 25°C after restart"
    elif p2_got and not recalls_25:
        verdict = "PARTIAL — replied after restart but didn't recall 25°C"
    elif not p1_noted:
        verdict = "FAIL — NoteMemory not called in phase 1"
    else:
        verdict = "FAIL — no response after restart"
    print(f"Verdict: {verdict}")
    print(f"Wall time: {wall:.1f}s")
    print(f"Phase 1 NoteMemory called: {p1_noted}")
    print(f"Phase 1 reply: {p1_say[:200]!r}")
    print(f"Phase 2 reply: {p2_say[:200]!r}")
    print(f"Phase 2 mentions 25: {recalls_25}")
    print(f"Log: {LOG_PATH}")

    return 0 if verdict.startswith("PASS") else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
