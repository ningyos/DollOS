"""WER (word error rate) via faster-whisper transcription + jiwer comparison."""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

_WHISPER_MODEL = "small"


def _normalize_for_wer(text: str) -> str:
    """Lowercase, drop punct, collapse whitespace, NFC unicode."""
    text = unicodedata.normalize("NFC", text)
    text = text.lower()
    text = re.sub(r"[.,!?;:'\"\(\)\[\]，。！？；：、「」『』]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class WERRunner:
    def __init__(self, model_name: str = _WHISPER_MODEL) -> None:
        self._model = None
        self._model_name = model_name

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        logger.info("loading faster-whisper %s ...", self._model_name)
        self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")

    def transcribe(self, wav_path: Path) -> str:
        self._ensure_loaded()
        segments, _info = self._model.transcribe(str(wav_path))
        return " ".join(seg.text for seg in segments)

    def score(self, synth_wav: Path, expected_text: str) -> float:
        """Return WER (0.0 = perfect)."""
        import jiwer

        hypothesis = _normalize_for_wer(self.transcribe(synth_wav))
        reference = _normalize_for_wer(expected_text)
        return float(jiwer.wer(reference, hypothesis))
