#!/usr/bin/env python3
"""Non-streaming synth to test if streaming chunk decode is the source of artifacts."""
from __future__ import annotations
import argparse, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPY = REPO / "character_packs/powdur/voice/fish/powdur_voice_wy4wfdjPnY0.npy"
TXT = REPO / "character_packs/powdur/voice/transcripts/wy4wfdjPnY0.txt"

TESTS = [
    "Okay so let me tell you what happened today.",
    "Dude this is actually crazy, like genuinely insane.",
    "Hold on, that sentence was about to be weird.",
]

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()

    from fish_tts import get_instance, VoiceProfile
    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)
    profile = VoiceProfile.load(str(NPY), text=TXT.read_text().strip())
    synth.set_references([profile])

    for i, text in enumerate(TESTS):
        wav_bytes = synth.synthesize(text)
        out = Path(f"/tmp/powdur_nonstream_{i}.wav")
        out.write_bytes(wav_bytes)
        print(f"[{i}] -> {out}  ({len(wav_bytes)} bytes)")

if __name__ == "__main__":
    main()
