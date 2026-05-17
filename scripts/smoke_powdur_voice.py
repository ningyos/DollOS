#!/usr/bin/env python3
"""Powdur voice smoke — synthesize 3 sentences using new multi-ref profile.

Run:
    cd /home/progcat/Projects/DollOS
    uv run --extra fish python scripts/smoke_powdur_voice.py
    # If GPU is busy (e.g. llama-server running):
    uv run --extra fish python scripts/smoke_powdur_voice.py --device cpu
"""
from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path

import tomllib

from dollos.voice.tts_fish import FishTTSEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK = REPO_ROOT / "character_packs" / "powdur"

SENTENCES = [
    "Okay so let me tell you what happened today.",
    "Dude this is actually crazy, like genuinely insane.",
    "Hold on, that sentence was about to be weird.",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Device for fish-tts model (default: cuda)")
    args = parser.parse_args()

    cfg = tomllib.loads((PACK / "voice/engine.toml").read_text())
    fish_cfg = cfg["tts"]["fish-tts"]
    paths = [PACK / p for p in fish_cfg["voice_profile_paths"]]
    transcripts = fish_cfg["transcripts"]

    # fish-tts singleton: pass device explicitly so CPU mode works when GPU is busy.
    from fish_tts import get_instance
    precision = "fp32" if args.device == "cpu" else "bf16"
    get_instance(device=args.device, precision=precision)

    print(f"Loading FishTTSEngine with {len(paths)} reference profiles (device={args.device})...")
    eng = FishTTSEngine(voice_profile_paths=paths, transcripts=transcripts)

    for i, text in enumerate(SENTENCES):
        print(f"\n[{i}] Synthesizing: {text!r}")
        chunks: list[bytes] = []
        async for chunk in eng.synthesize(text):
            chunks.append(chunk)
        raw = b"".join(chunks)
        duration = len(raw) / 2 / eng.sample_rate
        out = Path(f"/tmp/powdur_smoke_{i}.wav")
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(eng.sample_rate)
            w.writeframes(raw)
        print(f"  -> {out}  ({duration:.2f}s)")

    await eng.aclose()
    print("\nSmoke done.")


if __name__ == "__main__":
    asyncio.run(main())
