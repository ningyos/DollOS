#!/usr/bin/env python
"""smoke_mind_shell_and_read.py — Shell → ToolResultArrived → ReadToolOutput chain.

Sends: 跑 `seq 1 200` 然後告訴我第 150 行
Expects: Doll calls Shell(seq 1 200), result arrives as perception,
         Doll calls ReadToolOutput to page to line 150, then Says the answer.

Pass: Doll produces a Say that mentions "150" (the answer to the question).
Observational: what tools did she use, in what order?
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

LOG_PATH = Path("/tmp/smoke_mind_shell_and_read.log")
# Shell + two LLM iterations + ReadToolOutput + final Say
TIMEOUT_S = 180.0
PROMPT = "跑 `seq 1 200` 然後告訴我第 150 行是什麼"


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


async def _collect_say(ws, timeout_s: float, silence_s: float = 8.0) -> tuple[str, bool]:
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
    print(f"[smoke_shell] log → {LOG_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="doll-shell-"))
    print(f"[smoke_shell] data root → {tmp_root}")

    settings = _build_settings(tmp_root)
    dollos = DollOS(settings)
    dollos._bootstrapped_dates.add(_date.today())

    await dollos.memsearch.index()
    await dollos.server.start()

    mind_task = asyncio.create_task(dollos._mind_loop.run(), name="mind-loop")

    # Push Awoke but DON'T connect yet — let MindLoop process it in 1 iteration.
    import time as _time
    from dollos.mind.mind_state import Perception
    dollos._perception_queue.put(
        Perception(kind="Awoke", t=_time.time(), data={"reason": "cold_start"})
    )
    # Wait for Awoke iteration to finish before connecting WS
    await asyncio.sleep(15.0)

    result_text = ""
    got_reply = False
    try:
        port = dollos.server.port
        uri = f"ws://127.0.0.1:{port}"
        print(f"[smoke_shell] WS → {uri}")
        print(f"[smoke_shell] sending: {PROMPT!r}")

        async with websockets.connect(uri) as ws:
            iter_before = dollos._mind_state.iter_count
            await ws.send(json.dumps({"type": "text_input", "text": PROMPT}))
            # Wait for response — allow up to TIMEOUT_S, silence after first chunk
            result_text, got_reply = await _collect_say(
                ws, timeout_s=TIMEOUT_S, silence_s=15.0
            )
            print(f"[smoke_shell] iter_count delta: {dollos._mind_state.iter_count - iter_before}")
    finally:
        dollos._mind_loop.shutdown()
        await asyncio.gather(mind_task, return_exceptions=True)
        await dollos.server.stop()

    wall = time.monotonic() - t0

    # Inspect recent_outputs for tool sequence
    outputs = list(dollos._mind_state.recent_outputs)
    tool_seq = [o.kind for o in outputs]

    print("\n========== smoke_mind_shell_and_read RESULT ==========")
    mentions_150 = "150" in result_text
    if got_reply and mentions_150:
        verdict = "PASS"
    elif got_reply:
        verdict = "PARTIAL — replied but didn't mention 150"
    else:
        verdict = "FAIL — no Say within timeout"
    print(f"Verdict: {verdict}")
    print(f"Wall time: {wall:.1f}s")
    print(f"Doll said: {result_text[:400]!r}")
    print(f"Tool sequence: {tool_seq}")
    print(f"MindLoop iter count: {dollos._mind_state.iter_count}")
    print(f"Scratchpad: {dollos._mind_state.scratchpad!r}")
    print(f"Log: {LOG_PATH}")

    return 0 if (got_reply and mentions_150) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(130)
