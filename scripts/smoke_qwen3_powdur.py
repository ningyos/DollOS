#!/usr/bin/env python3
"""Qwen3-TTS Powdur smoke + acoustic diagnostic.

Loads the Qwen3-TTS engine via DollOS's TTS_REGISTRY, synthesises three
Powdur-flavoured sentences, writes wavs to /tmp, then runs the same RMS /
F0 / spectral-centroid comparison we ran for fish-tts so quality can be
checked numerically against the YouTube reference clip.

Run:
    cd .worktrees/qwen3-tts
    uv sync --extra qwen3        # one-time: pull qwen-tts + torch
    uv run python scripts/smoke_qwen3_powdur.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

# Ensure DollOS registers all voice engines (qwen3-tts included)
import dollos.voice  # noqa: F401
from dollos.voice.engines import TTS_REGISTRY
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "character_packs" / "powdur"
ENGINE_TOML = PACK_DIR / "voice" / "engine.toml"
REF_AUDIO = PACK_DIR / "voice" / "fish" / "powdur_voice.ref.wav"  # YT clip we cut from
OUT_DIR = Path("/tmp")

# DollOS-level infra: production code path picks the engine + model_id +
# device here, then merges with the pack's per-engine identity block.
DOLLOS_VOICE_TTS = {
    "engine": "qwen3-tts",
    "model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "device": "cuda:0",
}

TEXTS = [
    "Hi. I am Powdur, a daemon that lives in your computer.",
    "The GPU is warm today. Good for a nap.",
    "I made a new model in Blender. Want to see it?",
]


def load_engine_config(pack_dir: Path, dollos_voice_tts: dict) -> tuple[str, dict]:
    """Load the pack's voice config and merge with DollOS-level infra.

    Mirrors the production code path in ``dollos.kernel.build_voice_engines``.
    """
    cfg = load_voice_config(pack_dir)
    return resolve_voice_kwargs(cfg, dollos_voice_tts)


def write_wav(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


def acoustic_stats(path: Path) -> dict:
    """RMS / peak / spectral centroid / F0 median + IQR."""
    import soundfile as sf
    import librosa

    a, sr = sf.read(str(path), dtype="float32")
    if a.ndim == 2:
        a = a.mean(axis=1)
    duration = len(a) / sr
    rms = float(np.sqrt(np.mean(a ** 2)))
    peak = float(np.max(np.abs(a)))
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=a, sr=sr)))
    f0 = librosa.yin(a, fmin=80, fmax=500, sr=sr)
    f0 = f0[~np.isnan(f0)]
    f0 = f0[(f0 > 80) & (f0 < 500)]
    f0_median = float(np.median(f0)) if len(f0) else 0.0
    f0_iqr = float(np.percentile(f0, 75) - np.percentile(f0, 25)) if len(f0) else 0.0
    return {
        "duration": duration,
        "rms": rms,
        "peak": peak,
        "centroid_hz": centroid,
        "f0_median_hz": f0_median,
        "f0_iqr_hz": f0_iqr,
    }


async def main() -> None:
    if not ENGINE_TOML.exists():
        sys.exit(f"engine config not found: {ENGINE_TOML}")
    if not REF_AUDIO.exists():
        sys.exit(f"reference audio missing: {REF_AUDIO}")

    engine_name, cfg = load_engine_config(PACK_DIR, DOLLOS_VOICE_TTS)
    if engine_name not in TTS_REGISTRY:
        sys.exit(f"engine {engine_name!r} not registered; TTS_REGISTRY={sorted(TTS_REGISTRY)}")

    print(f"loading {engine_name} engine ({cfg.get('model_id', '?')}) ...", flush=True)
    t0 = time.perf_counter()
    eng = TTS_REGISTRY[engine_name](**cfg)
    print(f"loaded in {time.perf_counter() - t0:.1f}s", flush=True)

    sample_paths: list[Path] = []
    for i, text in enumerate(TEXTS):
        print(f"[{i}] synth: {text!r}", flush=True)
        t0 = time.perf_counter()
        chunks: list[bytes] = []
        async for c in eng.synthesize(text):
            chunks.append(c)
        dt = time.perf_counter() - t0
        pcm = b"".join(chunks)
        out = OUT_DIR / f"powdur_qwen3_{i}.wav"
        write_wav(out, pcm, eng.sample_rate)
        audio_s = len(pcm) / 2 / eng.sample_rate
        rtf = dt / audio_s if audio_s > 0 else float("inf")
        print(f"  {dt:.2f}s -> {audio_s:.2f}s audio -> RTF {rtf:.2f}x -> {out}", flush=True)
        sample_paths.append(out)

    print("\n=== acoustic comparison ===")
    header = f"{'file':<30}  {'dur':>5}  {'rms':>7}  {'peak':>6}  {'cent_Hz':>7}  {'f0_med':>6}  {'f0_iqr':>6}"
    print(header)
    print("-" * len(header))
    ref_stats = acoustic_stats(REF_AUDIO)
    print(f"{REF_AUDIO.name:<30}  {ref_stats['duration']:>4.2f}s  "
          f"{ref_stats['rms']:>7.4f}  {ref_stats['peak']:>6.4f}  "
          f"{ref_stats['centroid_hz']:>7.0f}  {ref_stats['f0_median_hz']:>6.0f}  "
          f"{ref_stats['f0_iqr_hz']:>6.0f}")
    for p in sample_paths:
        s = acoustic_stats(p)
        print(f"{p.name:<30}  {s['duration']:>4.2f}s  "
              f"{s['rms']:>7.4f}  {s['peak']:>6.4f}  "
              f"{s['centroid_hz']:>7.0f}  {s['f0_median_hz']:>6.0f}  "
              f"{s['f0_iqr_hz']:>6.0f}")
    print("\nGoal: samples should keep f0_iqr close to ref's (high IQR = expressive).")
    print("fish-tts S1-mini baseline (recorded earlier): f0_iqr 46-114 Hz, mostly under ref's 99 Hz.")


if __name__ == "__main__":
    asyncio.run(main())
