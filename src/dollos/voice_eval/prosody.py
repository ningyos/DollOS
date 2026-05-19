"""Prosody similarity — F0 distribution stats distance (semitones).

Ref and synth audio say different content, so contour alignment is
meaningless. Compare (mean, p10, p90, std) of voiced-frame F0 in
semitones; Euclidean distance between the two 4-D stat vectors.

Lower = closer pitch profile. Range typically 0-20 semitones.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _to_semitones(f0_hz: np.ndarray, ref_hz: float = 440.0) -> np.ndarray:
    """12 * log2(f0 / ref). NaN passes through."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return 12.0 * np.log2(f0_hz / ref_hz)


def _f0_voiced_semitones(wav_path: Path) -> np.ndarray:
    """Extract F0 with librosa.pyin, drop unvoiced frames, return semitones."""
    import librosa

    y, sr = librosa.load(wav_path, sr=16000, mono=True)
    f0, voiced_flag, _ = librosa.pyin(
        y,
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"),
        sr=16000,
    )
    # Drop NaN (unvoiced) frames
    f0_voiced = f0[~np.isnan(f0)]
    if len(f0_voiced) == 0:
        return np.array([], dtype=np.float64)
    return _to_semitones(f0_voiced)


def _stats(semitones: np.ndarray) -> tuple[float, float, float, float] | None:
    """Return (mean, p10, p90, std). None if input is empty."""
    if len(semitones) == 0:
        return None
    return (
        float(np.mean(semitones)),
        float(np.percentile(semitones, 10)),
        float(np.percentile(semitones, 90)),
        float(np.std(semitones)),
    )


class ProsodyRunner:
    """F0 distribution stats distance between ref and synth audio."""

    def score(self, ref_wav: Path, synth_wav: Path) -> float:
        """Euclidean distance between F0 stat vectors. NaN if either has no voiced frames."""
        ref_st = _f0_voiced_semitones(ref_wav)
        syn_st = _f0_voiced_semitones(synth_wav)
        ref_stats = _stats(ref_st)
        syn_stats = _stats(syn_st)
        if ref_stats is None or syn_stats is None:
            return float("nan")
        a = np.array(ref_stats)
        b = np.array(syn_stats)
        return float(np.linalg.norm(a - b))
