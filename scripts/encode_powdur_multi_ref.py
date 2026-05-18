#!/usr/bin/env python3
"""Encode all 10 Powdur transcript clips into fish-tts voice profiles.

Reads wav+txt pairs from character_packs/powdur/voice/transcripts/,
encodes each with the fish-tts singleton, and saves .npy files to
character_packs/powdur/voice/fish/.

Run:
    cd /home/progcat/Projects/DollOS
    uv run --extra fish python scripts/encode_powdur_multi_ref.py
    # If GPU is busy, run on CPU (slow but works):
    uv run --extra fish python scripts/encode_powdur_multi_ref.py --device cpu
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = REPO_ROOT / "character_packs" / "powdur" / "voice" / "transcripts"
OUT_DIR = REPO_ROOT / "character_packs" / "powdur" / "voice" / "fish"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Device for fish-tts model (default: cuda)")
    args = parser.parse_args()

    try:
        from fish_tts import get_instance
    except ModuleNotFoundError:
        sys.exit(
            "fish-tts not installed. Run: uv sync --extra fish"
        )

    precision = "fp32" if args.device == "cpu" else "bf16"
    synth = get_instance(device=args.device, precision=precision)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    wav_paths = sorted(TRANSCRIPTS_DIR.glob("*.wav"))
    if not wav_paths:
        sys.exit(f"No .wav files found in {TRANSCRIPTS_DIR}")

    print(f"Found {len(wav_paths)} clips. Encoding...\n")
    for wav_path in wav_paths:
        video_id = wav_path.stem
        txt_path = TRANSCRIPTS_DIR / f"{video_id}.txt"
        if not txt_path.exists():
            print(f"  [SKIP] {video_id} — no matching .txt file")
            continue

        transcript = txt_path.read_text(encoding="utf-8").strip()

        # EBU R128 loudness normalize to -16 LUFS / TP -1.5 / LRA 11 via
        # ffmpeg before encoding. Source YT clips have ~2x RMS variation
        # which biases fish-tts speaker signature. Normalize for consistency.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-ar", "44100", "-ac", "1", tmp_path],
                check=True, capture_output=True,
            )
            wav_bytes = Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        print(f"  [{video_id}] transcript: {transcript[:60]!r}...")
        profile = synth.encode_reference(wav_bytes, transcript)
        # profile is a VoiceProfile; use its own save() which stores codes only.
        out_path = OUT_DIR / f"powdur_voice_{video_id}.npy"
        profile.save(out_path)
        size_kb = out_path.stat().st_size / 1024
        print(f"    -> {out_path.name}  ({size_kb:.1f} KB)\n")

    print("Done.")
    print(f"Output: {OUT_DIR}")
    npy_files = sorted(OUT_DIR.glob("powdur_voice_*.npy"))
    print(f"  {len(npy_files)} .npy files written:")
    for f in npy_files:
        print(f"    {f.name}  ({f.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
