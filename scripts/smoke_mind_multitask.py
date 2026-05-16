#!/usr/bin/env python
"""smoke_mind_multitask.py — multi-task awareness via [Active tasks] block.

Sends two messages:
1. "幫我跑三件事: sleep 8, sleep 5, ls /tmp" — ask Doll to dispatch 3 Shell tasks
2. After a pause, "進度怎樣?" — ask about progress

Validates:
- Doll dispatches Shell commands (Shell tool calls observed)
- After querying progress, Doll references active/completed tasks (MindState awareness)

Pass: Doll dispatches Shell(s) on first message, and gives some progress update on second.
Observational: timing, which shells she dispatches, what she says about progress.
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

LOG_PATH = Path("/tmp/smoke_mind_multitask.log")
TIMEOUT_PER_TURN_S = 90.0
PROMPT_1 = "幫我跑三件事: sleep 8, sleep 5, ls /tmp"
PROMPT_2 = "進度怎樣?"


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


async def _collect_say(ws, timeout_s: float, silence_s: float = 10.0) -> tuple[str, bool]:
    """Collect text_chunk messages; stop after silence_s of silence following first chunk."""
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
        t = msg.get("type")
        if t == "text_chunk":
            chunks.append(msg.get("text", ""))
            got_any = True
    return "".join(chunks), got_any


async def _run() -> int:
    _setup_logging()
    t0 = time.monotonic()
    print(f"[smoke_multitask] log → {LOG_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-multi-"))
    print(f"[smoke_multitask] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()

    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    import time as _time
    from dollos.mind.mind_state import Perception
    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "cold_start"})
    )
    # Wait for Awoke iteration to settle
    await asyncio.sleep(12.0)

    turn1_text = ""
    turn1_reply = False
    turn2_text = ""
    turn2_reply = False
    tool_seq_1: list[str] = []
    tool_seq_2: list[str] = []

    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke_multitask] WS → {uri}")

        async with websockets.connect(uri) as ws:
            # Turn 1: dispatch 3 tasks
            print(f"[smoke_multitask] turn1: {PROMPT_1!r}")
            iter_before = dollos._mind_state.iter_count
            outputs_before = len(dollos._mind_state.recent_outputs)
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT_1}))
            turn1_text, turn1_reply = await _collect_say(ws, timeout_s=TIMEOUT_PER_TURN_S)
            outputs_after1 = len(dollos._mind_state.recent_outputs)
            tool_seq_1 = [o.kind for o in list(dollos._mind_state.recent_outputs)[outputs_before:outputs_after1]]
            print(f"[smoke_multitask] turn1 reply: {turn1_text[:150]!r}")
            print(f"[smoke_multitask] turn1 tools: {tool_seq_1}")

            # Wait a bit for tasks to be in progress (shell tasks running)
            await asyncio.sleep(8.0)

            # Turn 2: ask about progress
            print(f"[smoke_multitask] turn2: {PROMPT_2!r}")
            outputs_before2 = len(dollos._mind_state.recent_outputs)
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT_2}))
            turn2_text, turn2_reply = await _collect_say(ws, timeout_s=TIMEOUT_PER_TURN_S)
            outputs_after2 = len(dollos._mind_state.recent_outputs)
            tool_seq_2 = [o.kind for o in list(dollos._mind_state.recent_outputs)[outputs_before2:outputs_after2]]
            print(f"[smoke_multitask] turn2 reply: {turn2_text[:150]!r}")
            print(f"[smoke_multitask] turn2 tools: {tool_seq_2}")

    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    wall = time.monotonic() - t0
    dispatched_shells = sum(1 for k in tool_seq_1 if k == "Shell")

    print("\n========== smoke_mind_multitask RESULT ==========")
    if dispatched_shells >= 1 and turn2_reply:
        verdict = "PASS"
    elif turn1_reply and not (dispatched_shells >= 1):
        verdict = "PARTIAL — replied but didn't dispatch Shell commands"
    else:
        verdict = "FAIL"
    print(f"Verdict: {verdict}")
    print(f"Wall time: {wall:.1f}s")
    print(f"Turn 1 tools: {tool_seq_1}")
    print(f"Turn 1 said: {turn1_text[:200]!r}")
    print(f"Turn 2 tools: {tool_seq_2}")
    print(f"Turn 2 said: {turn2_text[:200]!r}")
    print(f"Shells dispatched in turn1: {dispatched_shells}")
    print(f"MindLoop iter count: {dollos._mind_state.iter_count}")
    print(f"Scratchpad: {dollos._mind_state.scratchpad!r}")
    print(f"Log: {LOG_PATH}")

    return 0 if (dispatched_shells >= 1 and turn2_reply) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
