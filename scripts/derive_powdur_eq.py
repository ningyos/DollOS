#!/usr/bin/env python3
"""Derive matching EQ curve from ref vs TTS output LTAS difference."""
from __future__ import annotations
import argparse
import subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import stft

REPO = Path(__file__).resolve().parent.parent
REF_DIR = REPO / "character_packs/powdur/voice/transcripts"
TTS_DIR = Path("/tmp/powdur_qwen3_j3D")
OUT_DIR = Path("/tmp/powdur_qwen3_j3D_eq")


def ltas(paths: list[Path], target_sr: int = 22050, n_fft: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    """Long-term average magnitude spectrum across multiple wavs."""
    mags = []
    for p in paths:
        x, sr = sf.read(p)
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr != target_sr:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(sr, target_sr)
            x = resample_poly(x, target_sr // g, sr // g)
            sr = target_sr
        f, _, Z = stft(x, fs=sr, nperseg=n_fft, noverlap=n_fft // 2)
        mag = np.abs(Z).mean(axis=1)
        mags.append(mag)
    return f, np.mean(mags, axis=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", nargs="+", default=None,
                    help="ref wavs; default = all transcripts/*.wav")
    ap.add_argument("--bands", type=int, default=16, help="number of EQ bands")
    args = ap.parse_args()

    ref_paths = [Path(p) for p in args.ref] if args.ref else sorted(REF_DIR.glob("*.wav"))
    tts_paths = sorted(TTS_DIR.glob("*.wav"))
    print(f"ref ({len(ref_paths)}): {[p.name for p in ref_paths]}")
    print(f"tts ({len(tts_paths)}): {[p.name for p in tts_paths]}")

    freqs, ref_ltas = ltas(ref_paths)
    _, tts_ltas = ltas(tts_paths)

    eps = 1e-10
    ref_db = 20 * np.log10(ref_ltas + eps)
    tts_db = 20 * np.log10(tts_ltas + eps)
    # Normalize each to its mean (overall loudness independent)
    ref_db -= ref_db.mean()
    tts_db -= tts_db.mean()
    diff_db = ref_db - tts_db  # what tts needs to add

    # Clip extremes
    diff_db = np.clip(diff_db, -15, 15)

    # Bin freqs into log-spaced bands and average diff per band
    fmin, fmax = 80, 10500
    band_edges = np.geomspace(fmin, fmax, args.bands + 1)
    centers = np.sqrt(band_edges[:-1] * band_edges[1:])
    gains = []
    for lo, hi in zip(band_edges[:-1], band_edges[1:]):
        idx = (freqs >= lo) & (freqs < hi)
        g = diff_db[idx].mean() if idx.any() else 0.0
        gains.append(g)
    gains = np.array(gains)

    print("\nEQ curve (center_hz, gain_db):")
    for c, g in zip(centers, gains):
        print(f"  {c:7.0f} Hz  {g:+5.2f} dB")

    # Build ffmpeg firequalizer curve: "f=hz:g=dB|f=hz:g=dB|..."
    curve = "|".join(f"entry({c:.0f},{g:.2f})" for c, g in zip(centers, gains))
    # firequalizer wants gain_entry='entry(f,g);entry(f,g);...'
    entries = ";".join(f"entry({c:.0f},{g:.2f})" for c, g in zip(centers, gains))
    print(f"\nffmpeg firequalizer gain_entry:\n{entries}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tts in tts_paths:
        out = OUT_DIR / f"{tts.stem}_match.wav"
        # 1. EQ via firequalizer to a temp wav
        tmp = OUT_DIR / f"{tts.stem}_eqtmp.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tts),
             "-af", f"firequalizer=gain_entry='{entries}'", str(tmp)],
            check=True,
        )
        # 2. Peak-normalize to 0.95 — preserves dynamics, no compression
        x, sr = sf.read(tmp)
        peak = float(np.max(np.abs(x))) if x.size else 1.0
        if peak > 1e-6:
            x = x * (0.95 / peak)
        sf.write(out, x, sr, subtype="PCM_16")
        tmp.unlink()
        print(f"  -> {out}")


if __name__ == "__main__":
    main()
