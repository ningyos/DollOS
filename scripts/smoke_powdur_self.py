#!/usr/bin/env python3
"""Sanity smoke: synth each ref clip's own transcript using that ref as voice profile.

If the output still doesn't sound like Powdur, the bottleneck is fish-tts itself
(or the source clip quality), not the inference text.
"""
from __future__ import annotations
import argparse, sys, wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NPY_DIR = REPO / "character_packs" / "powdur" / "voice" / "fish"
TXT_DIR = REPO / "character_packs" / "powdur" / "voice" / "transcripts"
OUT_DIR = Path("/tmp/powdur_self")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    args = ap.parse_args()

    try:
        from fish_tts import get_instance, VoiceProfile
    except ModuleNotFoundError:
        sys.exit("fish-tts not installed")

    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    npys = sorted(NPY_DIR.glob("powdur_voice_*.npy"))
    for npy in npys:
        vid = npy.stem.replace("powdur_voice_", "")
        txt = TXT_DIR / f"{vid}.txt"
        if not txt.exists():
            continue
        transcript = txt.read_text().strip()
        profile = VoiceProfile.load(str(npy), text=transcript)
        synth.set_references([profile])
        print(f"[{vid}] synth {len(transcript)} chars...")
        pcm = bytearray()
        for chunk in synth.synthesize_stream(transcript):
            pcm.extend(chunk)
        out = OUT_DIR / f"{vid}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(bytes(pcm))
        print(f"  -> {out}  ({len(pcm)/2/44100:.2f}s)")


if __name__ == "__main__":
    main()
