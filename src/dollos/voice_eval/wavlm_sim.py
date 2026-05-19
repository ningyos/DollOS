"""WavLM speaker similarity — cosine sim between WavLM x-vector embeddings
of reference and synthesized audio. Range [-1, 1]; near 1 = same speaker.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_ID = "microsoft/wavlm-base-plus-sv"


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    a = a.flatten().astype(np.float32)
    b = b.flatten().astype(np.float32)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class WavLMSimRunner:
    """Lazy-load WavLM once; score arbitrary (ref, synth) wav pairs."""

    def __init__(self) -> None:
        self._model = None
        self._processor = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from transformers import AutoFeatureExtractor, AutoModelForAudioXVector

        logger.info("loading %s ...", _MODEL_ID)
        self._processor = AutoFeatureExtractor.from_pretrained(_MODEL_ID)
        self._model = AutoModelForAudioXVector.from_pretrained(_MODEL_ID)
        self._model.eval()

    def _embed(self, wav_path: Path) -> np.ndarray:
        import soundfile as sf
        import torch

        self._ensure_loaded()
        audio, sr = sf.read(wav_path)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(sr, 16000)
            audio = resample_poly(audio, 16000 // g, sr // g)
        inputs = self._processor(audio, sampling_rate=16000, return_tensors="pt")
        with torch.no_grad():
            out = self._model(**inputs).embeddings
        return out.numpy()

    def score(self, ref_wav: Path, synth_wav: Path) -> float:
        ref_emb = self._embed(ref_wav)
        syn_emb = self._embed(synth_wav)
        return _cosine_sim(ref_emb, syn_emb)
