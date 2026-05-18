#!/usr/bin/env python3
"""Single-ref smoke: each of 9 clips × same test sentences."""
from __future__ import annotations
import argparse, sys, wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPY = REPO / "character_packs/powdur/voice/fish"
TXT = REPO / "character_packs/powdur/voice/transcripts"

VIDS = [
    "0uUmiDMPpEI", "KAigUNGWd38", "qCRz4eNbj4g", "wy4wfdjPnY0",
    "qMdkXP8ohw0", "j3DAXXUiGJw", "VckETuZqtPk", "p3akdeB4MTk", "5SK5G5kU7zY",
]
TESTS = [
    "Okay so let me tell you what happened today.",
    "Hey, look at my hat. Isn't it beautiful?",
    "Wait, why would you even do that?",
]
OUT = Path("/tmp/powdur_single")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()
    from fish_tts import get_instance, VoiceProfile
    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)
    OUT.mkdir(parents=True, exist_ok=True)

    for vid in VIDS:
        profile = VoiceProfile.load(str(NPY / f"powdur_voice_{vid}.npy"),
                                    text=(TXT / f"{vid}.txt").read_text().strip())
        synth.set_references([profile])
        print(f"=== {vid} ===")
        for i, text in enumerate(TESTS):
            pcm = bytearray()
            for chunk in synth.synthesize_stream(text, prefix_tokens=60):
                pcm.extend(chunk)
            out = OUT / f"{vid}_{i}.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                w.writeframes(bytes(pcm))
            print(f"  [{i}] -> {out.name}  ({len(pcm)/2/44100:.2f}s)")

if __name__ == "__main__":
    main()
