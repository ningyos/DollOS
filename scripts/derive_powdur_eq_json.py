#!/usr/bin/env python3
"""Derive EQ curve at 24 kHz target, write character_packs/powdur/voice/eq.json.

Variant of derive_powdur_eq.py:
  - target_sr = 24000 (Qwen3-TTS native output rate)
  - fmax     = 11500
  - emits JSON in the schema consumed by Qwen3TTSEngine
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import stft

REPO = Path(__file__).resolve().parent.parent
REF = REPO / "character_packs/powdur/voice/transcripts/j3DAXXUiGJw.wav"
TTS_DIR = Path("/tmp/powdur_qwen3_j3D")
OUT_JSON = REPO / "character_packs/powdur/voice/eq.json"

TARGET_SR = 24000
N_BANDS = 16
FMIN = 80
FMAX = 11500
N_FFT = 2048


def ltas(paths, target_sr: int = TARGET_SR, n_fft: int = N_FFT):
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
    ref_paths = [REF]
    tts_paths = sorted(TTS_DIR.glob("*.wav"))
    if not tts_paths:
        raise SystemExit(f"no synth wavs in {TTS_DIR} — run smoke_powdur_qwen3.py first")
    print(f"ref : {[p.name for p in ref_paths]}")
    print(f"tts : {len(tts_paths)} files in {TTS_DIR}")

    freqs, ref_ltas = ltas(ref_paths)
    _, tts_ltas = ltas(tts_paths)

    eps = 1e-10
    ref_db = 20 * np.log10(ref_ltas + eps); ref_db -= ref_db.mean()
    tts_db = 20 * np.log10(tts_ltas + eps); tts_db -= tts_db.mean()
    diff_db = np.clip(ref_db - tts_db, -15, 15)

    band_edges = np.geomspace(FMIN, FMAX, N_BANDS + 1)
    centers = np.sqrt(band_edges[:-1] * band_edges[1:])
    gains = []
    for lo, hi in zip(band_edges[:-1], band_edges[1:]):
        idx = (freqs >= lo) & (freqs < hi)
        gains.append(float(diff_db[idx].mean()) if idx.any() else 0.0)

    print("\nEQ curve (center_hz, gain_db):")
    for c, g in zip(centers, gains):
        print(f"  {c:7.0f} Hz  {g:+5.2f} dB")

    payload = {
        "name": "spectrum-match j3D",
        "sample_rate": TARGET_SR,
        "bands": [
            {"freq_hz": round(float(c), 0), "gain_db": round(float(g), 2)}
            for c, g in zip(centers, gains)
        ],
        "peak_normalize": 0.95,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
