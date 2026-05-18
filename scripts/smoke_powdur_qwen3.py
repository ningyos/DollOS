#!/usr/bin/env python3
"""Qwen3-TTS smoke for Powdur using g1/g2 single-ref."""
from __future__ import annotations
import argparse, asyncio, wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TXT_DIR = REPO / "character_packs/powdur/voice/transcripts"
WAV_DIR = REPO / "character_packs/powdur/voice/transcripts"

# (group_label, video_id) — longest cleanest single clip per group
GROUPS = [
    ("j3D", "j3DAXXUiGJw"),
]
TESTS = [
    "Okay so let me tell you what happened today.",
    "Hey, look at my hat. Isn't it beautiful?",
    "Wait, why would you even do that? That makes zero sense.",
    "Dude this is actually crazy, like genuinely insane.",
    "I had the weirdest dream last night, you would not believe it.",
    "Stop, stop, stop. We are not doing that today.",
    "I genuinely cannot believe people are still arguing about this on Twitter.",
    "Hold on, that sentence was about to be weird.",
    "Honestly, I think I might just go to bed early tonight.",
    "Oh my god, did you see what they posted? I cannot.",
]
OUT = Path("/tmp/powdur_qwen3")

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    OUT.mkdir(parents=True, exist_ok=True)
    for gname, vid in GROUPS:
        ref_audio = WAV_DIR / f"{vid}.wav"
        ref_text = (TXT_DIR / f"{vid}.txt").read_text().strip()
        engine = Qwen3TTSEngine(
            ref_audio=ref_audio,
            ref_text=ref_text,
            device=args.device,
            language="English",
        )
        print(f"=== {gname} ({vid}) ===")
        for i, text in enumerate(TESTS):
            pcm = bytearray()
            async for chunk in engine.synthesize(text):
                pcm.extend(chunk)
            out = OUT / f"{gname}_{i}.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(engine.sample_rate)
                w.writeframes(bytes(pcm))
            print(f"  [{i}] -> {out.name}  ({len(pcm)/2/engine.sample_rate:.2f}s)")

if __name__ == "__main__":
    asyncio.run(main())
