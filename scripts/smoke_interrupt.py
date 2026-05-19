"""smoke_interrupt.py — verify Say cancel + cascade preempt on new user input.

Scenario: send long-response prompt, wait for first text_chunk + 500ms grace,
interrupt with a new prompt. Observe SayAborted + second turn proceeds.
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


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://127.0.0.1:8001",
            model_alias="unsloth/Qwen3.6",
            timeout_s=120.0,
        ),
        ipc=IPCConfig(host="127.0.0.1", port=8769),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp / "data"),
        memsearch=MemsearchConfig(top_k=5),
        character=CharacterConfig(
            pack=str(REPO_ROOT / "character_packs" / "gura")
        ),
    )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dollos_int_") as tmp:
        dollos = DollOS(_make_settings(Path(tmp)))
        run_task = asyncio.create_task(dollos.run())
        await asyncio.sleep(3.0)

        chunks_before_interrupt: list[str] = []
        say_aborted = False
        chunks_after_interrupt: list[str] = []

        try:
            async with websockets.connect("ws://127.0.0.1:8769") as ws:
                # 1. Long prompt
                await ws.send(json.dumps({"type": "text_input", "text": "用六句話介紹自己。"}))
                print("\n→ sent long prompt", flush=True)

                # 2. Wait for first text_chunk + 500ms grace before interrupting
                first_chunk_seen = False
                end = time.monotonic() + 30.0
                grace_until: float | None = None
                while time.monotonic() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except asyncio.TimeoutError:
                        if grace_until and time.monotonic() >= grace_until:
                            break
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "text_chunk":
                        chunks_before_interrupt.append(msg["text"])
                        print(f"  pre: {msg['text']!r}", flush=True)
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            grace_until = time.monotonic() + 0.5
                if not first_chunk_seen:
                    print("WARNING: no text_chunk before timeout; interrupt won't test anything")
                    return 1

                # 3. Interrupt with new TextInput
                print("\n→ INTERRUPT with new prompt", flush=True)
                await ws.send(json.dumps({"type": "text_input", "text": "算了, 今天台北幾度?"}))

                # 4. Drain everything for 60s — expect SayAborted then second turn
                end = time.monotonic() + 60.0
                while time.monotonic() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "say_aborted":
                        say_aborted = True
                        print("  [say_aborted received]", flush=True)
                    elif msg.get("type") == "text_chunk":
                        chunks_after_interrupt.append(msg["text"])
                        print(f"  post: {msg['text']!r}", flush=True)
                    elif msg.get("type") == "turn_end":
                        print("  [turn_end]", flush=True)
                        break
                    else:
                        print(f"  [{msg.get('type')}]", flush=True)

        finally:
            dollos._mind_loop.shutdown()
            await asyncio.gather(run_task, return_exceptions=True)

    print("\n" + "=" * 60)
    print("OBSERVATIONS")
    print("=" * 60)
    pre_text = "".join(chunks_before_interrupt)
    post_text = "".join(chunks_after_interrupt)
    print(f"  text_chunks before interrupt: {len(chunks_before_interrupt)}")
    print(f"  say_aborted received:         {say_aborted}")
    print(f"  text_chunks after interrupt:  {len(chunks_after_interrupt)}")
    print()
    print(f"  pre  ({len(pre_text)} chars):  {pre_text[:200]!r}")
    print(f"  post ({len(post_text)} chars): {post_text[:300]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
