"""smoke_stale_subagent.py — empirical test: does Doll handle stale results gracefully?

Scenario: dispatch a long Shell, then have user shift topic for several turns,
let the Shell result land mid-conversation, observe Doll's reaction.

Question: does Doll spontaneously gracefully ignore / acknowledge the stale
result, or does she barge in with "hey here's the result of that thing from
5 minutes ago"?

If she handles it well → stale-result filter unnecessary (drop the task).
If she barges in → implement correlation_id + STALE marker.

Turns:
  T1: "Run a shell command `sleep 25 && echo done at $(date)`"
       → expect Doll to dispatch Shell
  (pause ~2s)
  T2: "Actually wait, what's 2+2?"
       → unrelated topic
  T3: "And what's the capital of France?"
       → continue unrelated
  T4: "Tell me a short joke"
       → continue unrelated
  (~20s later, Shell completes and ShellResultEvent fires)
  T5: (no user input, Doll wakes from result)
       → observe: does she barge in? acknowledge? ignore?

Pass criterion: subjective — read Doll's response at T5, decide if behaviour
needs the filter.
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

LOG_PATH = Path("/tmp/smoke_stale_subagent.log")
TIMEOUT_PER_TURN_S = 60.0
INTER_TURN_DELAY_S = 1.5

TURNS = [
    "Run a shell command: `sleep 25 && echo result_at_$(date +%s)`",
    "Actually, what's 2+2?",
    "And what's the capital of France?",
    "Tell me a short joke.",
]


def _setup_logging() -> None:
    LOG_PATH.write_text("")
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.INFO)


def _make_settings(data_root: Path) -> Settings:
    return Settings(
        log=LogConfig(level="INFO"),
        data=DataConfig(root=data_root),
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        memsearch=MemsearchConfig(top_k=5),
        ipc=IPCConfig(host="127.0.0.1", port=8765),
        character=CharacterConfig(pack=str(REPO_ROOT / "character_packs" / "gura")),
    )


async def send_and_collect(ws, text: str, label: str) -> list[str]:
    print(f"\n=== {label} → user: {text!r}")
    await ws.send(json.dumps({"type": "text_input", "text": text}))
    says: list[str] = []
    end = time.monotonic() + TIMEOUT_PER_TURN_S
    while time.monotonic() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            # may be idle between cascade iterations; keep waiting until turn marker
            continue
        msg = json.loads(raw)
        if msg.get("type") == "text_chunk":
            says.append(msg.get("text", ""))
            print(f"  Doll: {msg.get('text', '')!r}", flush=True)
        elif msg.get("type") == "turn_end":
            print("  [turn_end]", flush=True)
            break
        else:
            print(f"  [{msg.get('type')}] {raw[:200]}")
    return says


async def wait_passive(ws, seconds: float, label: str) -> list[dict]:
    """No user input — just collect any messages Doll emits (e.g. wakeup from ShellResult)."""
    print(f"\n=== {label} (passive {seconds}s) ===")
    msgs: list[dict] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        msgs.append(msg)
        if msg.get("type") == "text_chunk":
            print(f"  Doll: {msg.get('text', '')!r}", flush=True)
        else:
            print(f"  [{msg.get('type')}] {raw[:200]}", flush=True)
    return msgs


async def main() -> int:
    _setup_logging()
    with tempfile.TemporaryDirectory(prefix="dollos_stale_") as tmp:
        settings = _make_settings(Path(tmp))
        dollos = DollOS(settings)
        run_task = asyncio.create_task(dollos.run())
        # wait for IPC to be ready
        await asyncio.sleep(3.0)

        try:
            async with websockets.connect("ws://127.0.0.1:8765") as ws:
                # T1: dispatch shell
                t1 = await send_and_collect(ws, TURNS[0], "T1 dispatch")
                await asyncio.sleep(INTER_TURN_DELAY_S)

                # T2-T4: unrelated topics (shell still running 25s in background)
                for i, txt in enumerate(TURNS[1:], 2):
                    await send_and_collect(ws, txt, f"T{i} unrelated")
                    await asyncio.sleep(INTER_TURN_DELAY_S)

                # passive wait for Shell to complete (was 25s, ~8-10s into it by now)
                # then observe what Doll does when result lands
                passive_msgs = await wait_passive(ws, 30.0, "T5 passive — wait for Shell result")

                # Summary for human read
                print("\n" + "=" * 60)
                print("OBSERVATION:")
                print("  Did Doll spontaneously bring up the shell result? Look at T5 Says above.")
                print("  Look at her reaction tone — graceful integration or jarring interruption?")
                print("=" * 60)
        finally:
            dollos._mind_loop.shutdown()
            await asyncio.gather(run_task, return_exceptions=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
