"""Boot DollOS with voice_first output; send 3 prompts; observe text_chunk + tool dispatch."""
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

LOG_PATH = Path("/tmp/smoke_voice_first.log")
TIMEOUT_PER_TURN_S = 60.0
INTER_TURN_DELAY_S = 1.0

PROMPTS = [
    "你好, Doll",
    "今天台北氣溫是幾度?",
    "幫我記下: 明天要吃早餐",
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
        ipc=IPCConfig(host="127.0.0.1", port=8767),
        log=LogConfig(level="INFO"),
        data=DataConfig(root=tmp / "data"),
        memsearch=MemsearchConfig(top_k=5),
        character=CharacterConfig(
            pack=str(REPO_ROOT / "character_packs" / "gura")
        ),
    )


async def send_and_drain(ws, text: str, label: str) -> dict:
    print(f"\n=== {label} → {text!r}", flush=True)
    await ws.send(json.dumps({"type": "text_input", "text": text}))
    speak_segments: list[str] = []
    end = time.monotonic() + TIMEOUT_PER_TURN_S
    while time.monotonic() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("type") == "text_chunk":
            t = msg.get("text", "")
            speak_segments.append(t)
            print(f"  speak: {t!r}", flush=True)
        elif msg.get("type") == "turn_end":
            print("  [turn_end]", flush=True)
            return {"speak_segments": speak_segments}
        else:
            print(f"  [{msg.get('type')}]", flush=True)
    return {"speak_segments": speak_segments, "timeout": True}


async def main() -> int:
    _setup_logging()
    with tempfile.TemporaryDirectory(prefix="dollos_vf_") as tmp:
        dollos = DollOS(_make_settings(Path(tmp)))
        run_task = asyncio.create_task(dollos.run())
        await asyncio.sleep(3.0)

        results = []
        try:
            async with websockets.connect("ws://127.0.0.1:8767") as ws:
                for p in PROMPTS:
                    r = await send_and_drain(ws, p, f"T{len(results)+1}")
                    results.append(r)
                    await asyncio.sleep(INTER_TURN_DELAY_S)
        finally:
            dollos._mind_loop.shutdown()
            await asyncio.gather(run_task, return_exceptions=True)

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for i, r in enumerate(results):
        n = len(r["speak_segments"])
        timeout = r.get("timeout", False)
        print(f"  T{i+1}: {n} text_chunk segment(s)" + (" TIMEOUT" if timeout else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
