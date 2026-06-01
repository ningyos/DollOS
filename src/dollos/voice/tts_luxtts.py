"""LuxTTSEngine — TTS via luxtts-onnx with per-character voice clone prompt.

luxtts-onnx's `generate()` is synchronous, returns a full float32 array
at 48 kHz. We wrap it in asyncio.to_thread and chunk the output into
20ms PCM frames for the streaming ABC.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import numpy as np
from luxtts_onnx import LuxTTSOnnx

from dollos.voice.engines import TTSEngine, register_tts
from dollos.voice.eq import load_eq_curve

logger = logging.getLogger(__name__)


_SAMPLE_RATE = 48000
_FRAME_MS = 20
_SAMPLES_PER_CHUNK = _SAMPLE_RATE * _FRAME_MS // 1000  # 960 samples
_CHUNK_BYTES = _SAMPLES_PER_CHUNK * 2  # 2 bytes per int16 sample → 1920


def _pcm_chunks(audio_f32: np.ndarray) -> Iterator[bytes]:
    """Convert float32 [-1, 1] audio at 48 kHz into 20ms int16 PCM byte chunks."""
    clipped = np.clip(audio_f32, -1.0, 1.0)
    pcm_i16 = (clipped * 32767.0).astype(np.int16)
    raw = pcm_i16.tobytes()
    for i in range(0, len(raw), _CHUNK_BYTES):
        chunk = raw[i:i + _CHUNK_BYTES]
        if len(chunk) < _CHUNK_BYTES:
            chunk = chunk + b"\x00" * (_CHUNK_BYTES - len(chunk))
        yield chunk


@register_tts("luxtts-onnx")
class LuxTTSEngine(TTSEngine):
    """TTS engine wrapping luxtts-onnx for streaming PCM output."""

    sample_rate = _SAMPLE_RATE

    def __init__(
        self,
        *,
        model_dir: Path,
        prompt_path: Path,
        data_root: Path,
        device: str = "cpu",
        num_steps: int = 8,
        t_shift: float = 0.9,
        guidance_scale: float = 3.0,
        speed: float = 1.0,
        peak_target: float = 0.95,
        eq_curve_path: Path | str | None = None,
    ) -> None:
        self._speed = speed
        self._peak_target = float(peak_target)
        self._eq_fir = None
        self._eq_peak_normalize: float | None = None
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"luxtts prompt file not found: {prompt_path}; "
                f"run `python -m dollos.voice.prepare` to encode one"
            )
        if eq_curve_path is not None:
            eq_curve_path = Path(eq_curve_path)
            if not eq_curve_path.exists():
                raise FileNotFoundError(
                    f"luxtts eq_curve_path not found: {eq_curve_path}"
                )
            fir, eq_peak = load_eq_curve(eq_curve_path, self.sample_rate)
            self._eq_fir = fir
            self._eq_peak_normalize = eq_peak
            logger.info(
                "luxtts EQ loaded from %s (%d taps, peak_normalize=%.3f)",
                eq_curve_path, len(fir), eq_peak,
            )
        provider = "cuda" if device == "cuda" else "cpu"
        self._tts = LuxTTSOnnx(model_dir=str(model_dir), provider=provider)
        self._prompt = self._tts.load_prompt(str(prompt_path))
        self._num_steps = num_steps
        self._t_shift = t_shift
        self._guidance_scale = guidance_scale

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        audio = await asyncio.to_thread(
            self._tts.generate,
            text,
            self._prompt,
            self._num_steps,
            self._t_shift,
            self._guidance_scale,
            self._speed,
        )
        if self._eq_fir is not None:
            # Apply spectrum-match FIR EQ, then EQ-defined peak normalize.
            from scipy.signal import lfilter
            audio = lfilter(self._eq_fir, 1.0, audio).astype(np.float32)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            target = self._eq_peak_normalize or self._peak_target
            if peak > 1e-6:
                audio = audio * (target / peak)
        else:
            # Peak-normalize to bring luxtts output up to a consistent loudness.
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak > 1e-6:
                audio = audio * (self._peak_target / peak)
        for chunk in _pcm_chunks(audio):
            yield chunk

    async def aclose(self) -> None:
        self._tts = None
        self._prompt = None
