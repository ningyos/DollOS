#!/usr/bin/env python3
"""Smoke: Powdur pack + qwen3-tts with EQ baked into the engine.

Loads the Powdur voice pack via dollos.voice.pack, instantiates
Qwen3TTSEngine with the pack-derived kwargs (including eq_curve_path),
synthesizes 3 sentences and writes WAVs to /tmp/powdur_eq_baked/.
"""
from __future__ import annotations
import argparse
import asyncio
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "character_packs/powdur"
OUT = Path("/tmp/powdur_eq_baked")

TESTS = [
    "Okay so let me tell you what happened today.",
    "Hey, look at my hat. Isn't it beautiful?",
    "Wait, why would you even do that? That makes zero sense.",
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    cfg = load_voice_config(PACK)
    assert cfg.tts and "qwen3-tts" in cfg.tts, "powdur pack missing [tts.qwen3-tts]"
    engine_name, kwargs = resolve_voice_kwargs(
        cfg, {"engine": "qwen3-tts", "device": args.device}
    )
    assert engine_name == "qwen3-tts"
    print(f"engine kwargs: {kwargs}")
    engine = Qwen3TTSEngine(**kwargs)

    OUT.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(TESTS):
        pcm = bytearray()
        async for chunk in engine.synthesize(text):
            pcm.extend(chunk)
        out = OUT / f"baked_{i}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(engine.sample_rate)
            w.writeframes(bytes(pcm))
        print(f"[{i}] -> {out}  ({len(pcm)/2/engine.sample_rate:.2f}s @ {engine.sample_rate}Hz)")


if __name__ == "__main__":
    asyncio.run(main())
