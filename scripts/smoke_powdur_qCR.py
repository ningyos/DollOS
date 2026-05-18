#!/usr/bin/env python3
"""Fish-tts smoke with 0uUmiDMPpEI single-ref × 10 sentences."""
from __future__ import annotations
import argparse, sys, wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPY = REPO / "character_packs/powdur/voice/fish/powdur_voice_0uUmiDMPpEI.npy"
TXT = REPO / "character_packs/powdur/voice/transcripts/0uUmiDMPpEI.txt"

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
OUT = Path("/tmp/powdur_u0M")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()
    from fish_tts import get_instance, VoiceProfile
    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)
    OUT.mkdir(parents=True, exist_ok=True)

    profile = VoiceProfile.load(str(NPY), text=TXT.read_text().strip())
    synth.set_references([profile])

    for i, text in enumerate(TESTS):
        wav_bytes = synth.synthesize(text)
        out = OUT / f"u0M_{i}.wav"
        out.write_bytes(wav_bytes)
        print(f"[{i}] -> {out.name}", flush=True)

if __name__ == "__main__":
    main()
