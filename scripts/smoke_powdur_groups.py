#!/usr/bin/env python3
"""Smoke both Powdur voice groups separately."""
from __future__ import annotations
import argparse, sys, wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPY = REPO / "character_packs/powdur/voice/fish"
TXT = REPO / "character_packs/powdur/voice/transcripts"

GROUPS = {
    "g1": ["0uUmiDMPpEI", "KAigUNGWd38", "qCRz4eNbj4g", "wy4wfdjPnY0"],
    "g2": ["5SK5G5kU7zY", "j3DAXXUiGJw", "p3akdeB4MTk", "qMdkXP8ohw0", "VckETuZqtPk"],
}
TESTS = [
    "Okay so let me tell you what happened today.",
    "Dude this is actually crazy, like genuinely insane.",
    "Hold on, that sentence was about to be weird.",
    "I genuinely cannot believe people are still arguing about this on Twitter.",
    "Hey, look at my hat. Isn't it beautiful?",
    "Wait, why would you even do that? That makes zero sense.",
    "I had the weirdest dream last night, you would not believe it.",
    "Stop, stop, stop. We are not doing that today.",
]

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()
    from fish_tts import get_instance, VoiceProfile
    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)

    for gname, vids in GROUPS.items():
        profiles = [
            VoiceProfile.load(str(NPY / f"powdur_voice_{v}.npy"),
                              text=(TXT / f"{v}.txt").read_text().strip())
            for v in vids
        ]
        synth.set_references(profiles)
        print(f"=== group {gname} ({len(vids)} refs) ===")
        for i, text in enumerate(TESTS):
            pcm = bytearray()
            for chunk in synth.synthesize_stream(text, prefix_tokens=60):
                pcm.extend(chunk)
            out = Path(f"/tmp/powdur_{gname}_{i}.wav")
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                w.writeframes(bytes(pcm))
            print(f"  [{i}] -> {out}  ({len(pcm)/2/44100:.2f}s)")

if __name__ == "__main__":
    main()
