"""Qwen3TTSEngine — TTS via Qwen3-TTS (Alibaba) in-process.

Voice cloning via `model.generate_voice_clone(text, language, ref_audio, ref_text)`.
Emotion / style control via natural-language `instruction` text prefixed to the
input (Qwen3-TTS conditions on the leading text describing tone).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from threading import Lock

from dollos.voice.engines import TTSEngine, register_tts

logger = logging.getLogger(__name__)

_FRAME_MS = 20


def _rechunk_int16(raw: bytes, sample_rate: int) -> Iterator[bytes]:
    samples_per_chunk = sample_rate * _FRAME_MS // 1000
    chunk_bytes = samples_per_chunk * 2
    for i in range(0, len(raw), chunk_bytes):
        c = raw[i:i + chunk_bytes]
        if len(c) < chunk_bytes:
            c = c + b"\x00" * (chunk_bytes - len(c))
        yield c


# Module-level singleton — first construct loads the model; subsequent
# constructs (different characters) reuse it.
_MODEL_LOCK = Lock()
_MODEL = None
_MODEL_ID = None


def _get_model(model_id: str, device: str):
    global _MODEL, _MODEL_ID
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_ID != model_id:
            try:
                from qwen_tts import Qwen3TTSModel
            except ModuleNotFoundError as e:
                raise ModuleNotFoundError(
                    "qwen3-tts backend requires `qwen-tts`. Install with "
                    "`uv sync --extra qwen3`."
                ) from e
            logger.info("loading Qwen3-TTS model %s on %s ...", model_id, device)
            _MODEL = Qwen3TTSModel.from_pretrained(model_id, device_map=device)
            _MODEL_ID = model_id
        return _MODEL


@register_tts("qwen3-tts")
class Qwen3TTSEngine(TTSEngine):
    """TTS engine wrapping Qwen3-TTS with voice cloning + emotion instruction."""

    def __init__(
        self,
        *,
        model_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device: str = "cuda:0",
        ref_audio: Path,
        ref_text: str,
        language: str = "English",
        instruction: str = "",
    ) -> None:
        if not Path(ref_audio).exists():
            raise FileNotFoundError(f"Qwen3-TTS ref_audio not found: {ref_audio}")
        self._model = _get_model(model_id, device)
        self._ref_audio = str(ref_audio)
        self._ref_text = ref_text
        self._language = language
        self._instruction = instruction
        # Sample rate is whatever the model returns on first synthesize;
        # we initialise with 24000 (qwen-tts default codec rate). Re-set
        # dynamically on first synthesis.
        self.sample_rate = 24000

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        # Compose input: if the package supports a separate `instruction`
        # kwarg use it; otherwise prepend to the text body. Try kwarg first,
        # fall back to prefix on TypeError.
        prefixed_text = (
            f"{self._instruction}. {text}" if self._instruction else text
        )

        def _generate():
            try:
                return self._model.generate_voice_clone(
                    text=text,
                    language=self._language,
                    ref_audio=self._ref_audio,
                    ref_text=self._ref_text,
                    instruction=self._instruction or None,
                )
            except TypeError:
                # qwen-tts older signature without `instruction` kwarg.
                return self._model.generate_voice_clone(
                    text=prefixed_text,
                    language=self._language,
                    ref_audio=self._ref_audio,
                    ref_text=self._ref_text,
                )

        wavs, sr = await asyncio.to_thread(_generate)
        self.sample_rate = int(sr)
        # wavs is a (B, N) int16 numpy array OR list of arrays. Take first.
        import numpy as np
        wave = wavs[0] if hasattr(wavs, "__getitem__") else wavs
        if hasattr(wave, "astype"):
            pcm = wave.astype(np.int16, copy=False).tobytes()
        else:
            pcm = bytes(wave)
        for chunk in _rechunk_int16(pcm, self.sample_rate):
            yield chunk

    async def aclose(self) -> None:
        # Drop our handle — singleton model stays alive for other characters.
        pass
