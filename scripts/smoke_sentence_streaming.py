"""smoke_sentence_streaming.py — verify sentence-bounded streaming + measure first-audio latency.

Asks Doll for a multi-sentence reply, prints per-turn:
- time to first text_chunk (should be ~ first sentence generation time, much less than total)
- total time to turn_end
- count of text_chunks (each is a complete sentence)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dollos.config import (
    CharacterConfig, DataConfig, IPCConfig, LLMConfig, LogConfig,
    MemsearchConfig, Settings,
)
from dollos.kernel import DollOS

LOG_PATH = Path("/tmp/smoke_sentence_streaming.log")
TIMEOUT_PER_TURN_S = 60.0
INTER_TURN_DELAY_S = 1.0

PROMPTS = [
    "用三句話介紹自己。",
    "今天天氣不錯。你最喜歡的食物是什麼？我猜是甜的。",
]


def _setup_logging() -> None:
    LOG_PATH.write_text("")
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.INFO)


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        ipc=IPCConfig(host="127.0.0.1", port=8768),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp / "data"),
        memsearch=MemsearchConfig(top_k=5),
        character=CharacterConfig(
            pack=str(REPO_ROOT / "character_packs" / "gura")
        ),
    )


async def send_and_measure(ws, text: str, label: str) -> dict:
    print(f"\n=== {label} → {text!r}", flush=True)
    t_send = time.monotonic()
    await ws.send(json.dumps({"type": "text_input", "text": text}))
    chunks: list[str] = []
    t_first_chunk: float | None = None
    t_turn_end: float | None = None
    end = t_send + TIMEOUT_PER_TURN_S
    while time.monotonic() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("type") == "text_chunk":
            t = msg.get("text", "")
            if t_first_chunk is None:
                t_first_chunk = time.monotonic()
            chunks.append(t)
            print(f"  speak: {t!r}", flush=True)
        elif msg.get("type") == "turn_end":
            t_turn_end = time.monotonic()
            print("  [turn_end]", flush=True)
            break
        else:
            print(f"  [{msg.get('type')}]", flush=True)
    return {
        "label": label,
        "chunk_count": len(chunks),
        "first_chunk_latency_s": (t_first_chunk - t_send) if t_first_chunk else None,
        "total_s": (t_turn_end - t_send) if t_turn_end else None,
        "timeout": t_turn_end is None,
    }


async def main() -> int:
    _setup_logging()
    with tempfile.TemporaryDirectory(prefix="dollos_ss_") as tmp:
        dollos = DollOS(_make_settings(Path(tmp)))
        run_task = asyncio.create_task(dollos.run())
        await asyncio.sleep(3.0)

        results = []
        try:
            async with websockets.connect("ws://127.0.0.1:8768") as ws:
                for i, p in enumerate(PROMPTS, 1):
                    r = await send_and_measure(ws, p, f"T{i}")
                    results.append(r)
                    await asyncio.sleep(INTER_TURN_DELAY_S)
        finally:
            dollos._mind_loop.shutdown()
            await asyncio.gather(run_task, return_exceptions=True)

    print("\n" + "=" * 60)
    print("BENCHMARK")
    print("=" * 60)
    for r in results:
        first = f"{r['first_chunk_latency_s']:.2f}s" if r["first_chunk_latency_s"] else "N/A"
        total = f"{r['total_s']:.2f}s" if r["total_s"] else "TIMEOUT"
        print(f"  {r['label']}: first_chunk={first}  total={total}  chunks={r['chunk_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
