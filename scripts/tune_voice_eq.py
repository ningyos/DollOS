#!/usr/bin/env python3
"""Derive a per-character EQ curve by spectrum-matching synth output to ref audio.

Pack-agnostic: reads the pack's ``voice/engine.toml`` to figure out which TTS
engine to instantiate and which reference clip to match against, synthesizes a
fixed English test corpus, computes LTAS over ref vs. synth, and writes
``<pack>/voice/eq.json`` (16 log-spaced bands, 80 Hz → engine_sr/2 - margin).

Usage::

    uv run python scripts/tune_voice_eq.py character_packs/<pack>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import wave
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly, stft

from dollos.voice_eval.synth_driver import TEST_CORPUS


REPO = Path(__file__).resolve().parent.parent

N_BANDS = 16
FMIN = 80.0
FMAX_MARGIN_HZ = 500.0  # keep fmax slightly below Nyquist
N_FFT = 2048


# -----------------------------------------------------------------------------
# Reference clip + engine kwargs discovery
# -----------------------------------------------------------------------------


def _find_fish_ref_wav(npy_path: Path) -> Path:
    """Locate ref audio next to a fish-tts ``.npy`` voice profile.

    Heuristic: look for a wav with the same stem in ``voice/transcripts/``
    relative to the pack root (two levels up from ``voice/fish/x.npy``).
    """
    npy_path = Path(npy_path)
    stem = npy_path.stem
    # Strip a common prefix like "powdur_voice_" → keep the youtube ID.
    if "_" in stem:
        candidate_id = stem.rsplit("_", 1)[-1]
    else:
        candidate_id = stem
    # voice/fish/x.npy → pack/voice/
    voice_dir = npy_path.parent.parent
    transcripts_dir = voice_dir / "transcripts"
    for cand in (
        transcripts_dir / f"{candidate_id}.wav",
        transcripts_dir / f"{stem}.wav",
    ):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"could not locate reference wav next to {npy_path}; "
        f"looked in {transcripts_dir} for {candidate_id}.wav / {stem}.wav"
    )


def _discover_ref_and_kwargs(
    engine_name: str, kwargs: dict
) -> tuple[Path, dict]:
    """Return ``(ref_wav_path, kwargs_for_engine_without_eq)``.

    Strips ``eq_curve_path`` from kwargs so the engine produces raw,
    un-EQ'd output (we're re-deriving the EQ — don't double-correct).
    """
    stripped = {k: v for k, v in kwargs.items() if k != "eq_curve_path"}
    if engine_name == "qwen3-tts":
        ref = stripped.get("ref_audio")
        ref_text = stripped.get("ref_text")
        if ref is None or ref_text is None:
            raise ValueError(
                "engine qwen3-tts: pack must define ref_audio + ref_text "
                "in [tts.qwen3-tts] to tune EQ"
            )
        return Path(ref), stripped
    if engine_name == "fish-tts":
        paths = stripped.get("voice_profile_paths") or (
            [stripped["voice_profile_path"]]
            if stripped.get("voice_profile_path") is not None
            else None
        )
        if not paths:
            raise ValueError(
                "engine fish-tts: pack must define voice_profile_paths "
                "(or voice_profile_path) to tune EQ"
            )
        ref_wav = _find_fish_ref_wav(Path(paths[0]))
        return ref_wav, stripped
    raise ValueError(
        f"engine {engine_name!r} has no reference audio to tune EQ against"
    )


# -----------------------------------------------------------------------------
# LTAS / EQ derivation (lifted from derive_powdur_eq_json.py)
# -----------------------------------------------------------------------------


def _ltas(paths: list[Path], target_sr: int, n_fft: int = N_FFT):
    mags = []
    for p in paths:
        x, sr = sf.read(str(p))
        if x.ndim > 1:
            x = x.mean(axis=1)
        if sr != target_sr:
            g = gcd(sr, target_sr)
            x = resample_poly(x, target_sr // g, sr // g)
            sr = target_sr
        f, _, Z = stft(x, fs=sr, nperseg=n_fft, noverlap=n_fft // 2)
        mag = np.abs(Z).mean(axis=1)
        mags.append(mag)
    return f, np.mean(mags, axis=0)


def _derive_eq(
    ref_wavs: list[Path],
    synth_wavs: list[Path],
    target_sr: int,
) -> tuple[list[float], list[float]]:
    fmax = max(FMIN * 2, target_sr / 2.0 - FMAX_MARGIN_HZ)
    freqs, ref_ltas = _ltas(ref_wavs, target_sr=target_sr)
    _, syn_ltas = _ltas(synth_wavs, target_sr=target_sr)

    eps = 1e-10
    ref_db = 20 * np.log10(ref_ltas + eps); ref_db -= ref_db.mean()
    syn_db = 20 * np.log10(syn_ltas + eps); syn_db -= syn_db.mean()
    diff_db = np.clip(ref_db - syn_db, -15, 15)

    band_edges = np.geomspace(FMIN, fmax, N_BANDS + 1)
    centers = np.sqrt(band_edges[:-1] * band_edges[1:])
    gains: list[float] = []
    for lo, hi in zip(band_edges[:-1], band_edges[1:]):
        idx = (freqs >= lo) & (freqs < hi)
        gains.append(float(diff_db[idx].mean()) if idx.any() else 0.0)
    return [float(c) for c in centers], gains


# -----------------------------------------------------------------------------
# Synth driver
# -----------------------------------------------------------------------------


async def _synth_corpus(engine, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    wavs: list[Path] = []
    for i, text in enumerate(TEST_CORPUS):
        pcm = bytearray()
        async for chunk in engine.synthesize(text):
            pcm.extend(chunk)
        p = out_dir / f"synth_{i:02d}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(engine.sample_rate)
            w.writeframes(bytes(pcm))
        print(f"  [{i+1:2d}/{len(TEST_CORPUS)}] synth -> {p.name} "
              f"({len(pcm)/2/engine.sample_rate:.2f}s)")
        wavs.append(p)
    return wavs


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


async def amain(pack_dir: Path) -> None:
    from dollos.voice import tts_fish, tts_qwen3  # noqa: F401  (register engines)
    from dollos.voice.engines import TTS_REGISTRY
    from dollos.voice.pack import load_voice_config

    cfg = load_voice_config(pack_dir)
    if not cfg.tts:
        raise SystemExit(
            f"{pack_dir} has no [tts.*] section in voice/engine.toml"
        )
    if len(cfg.tts) != 1:
        # When multiple engines defined, pick the first; print a note.
        print(f"note: pack defines {len(cfg.tts)} engines; using the first")
    engine_name, raw_kwargs = next(iter(cfg.tts.items()))
    if engine_name not in TTS_REGISTRY:
        raise SystemExit(
            f"engine {engine_name!r} not registered in TTS_REGISTRY"
        )

    ref_wav, kwargs = _discover_ref_and_kwargs(engine_name, raw_kwargs)
    print(f"pack       : {pack_dir}")
    print(f"engine     : {engine_name}")
    print(f"ref wav    : {ref_wav}")
    print(f"engine kw  : { {k: (str(v) if isinstance(v, Path) else v) for k, v in kwargs.items()} }")

    engine_cls = TTS_REGISTRY[engine_name]
    engine = engine_cls(**kwargs)

    with tempfile.TemporaryDirectory(prefix=f"tune_eq_{pack_dir.name}_") as td:
        td_path = Path(td)
        print(f"synth dir  : {td_path}")
        synth_wavs = await _synth_corpus(engine, td_path)
        target_sr = engine.sample_rate
        print(f"sample_rate: {target_sr} Hz (engine native)")
        try:
            await engine.aclose()
        except Exception:
            pass

        centers, gains = _derive_eq([ref_wav], synth_wavs, target_sr=target_sr)

    print("\nEQ curve (center_hz, gain_db):")
    for c, g in zip(centers, gains):
        print(f"  {c:7.0f} Hz  {g:+5.2f} dB")

    payload = {
        "name": f"spectrum-match {pack_dir.name}",
        "sample_rate": int(target_sr),
        "bands": [
            {"freq_hz": round(float(c), 0), "gain_db": round(float(g), 2)}
            for c, g in zip(centers, gains)
        ],
        "peak_normalize": 0.95,
    }
    out_json = pack_dir / "voice" / "eq.json"
    out_json.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nwrote {out_json}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pack_dir", type=Path, help="path to character pack root")
    args = ap.parse_args()
    pack_dir = args.pack_dir.resolve()
    if not pack_dir.is_dir():
        raise SystemExit(f"not a directory: {pack_dir}")
    asyncio.run(amain(pack_dir))


if __name__ == "__main__":
    main()
