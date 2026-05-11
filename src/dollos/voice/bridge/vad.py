"""SileroVAD — voice activity detection via silero_vad.onnx (no torch).

Auto-downloads silero_vad.onnx (~2.3 MB) from HuggingFace into
<data_root>/voice/vad/ on first construction. The model state is a small
LSTM hidden state that must be threaded between chunks; we keep it
inside the instance.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download

logger = logging.getLogger(__name__)


_HF_REPO = "onnx-community/silero-vad"
_HF_FILENAME = "onnx/model.onnx"
_LOCAL_FILENAME = "silero_vad.onnx"


def _ensure_model(data_root: Path) -> Path:
    """Download silero_vad.onnx to <data_root>/voice/vad/ if missing."""
    vad_dir = data_root / "voice" / "vad"
    vad_dir.mkdir(parents=True, exist_ok=True)
    target = vad_dir / _LOCAL_FILENAME
    if target.exists() and target.stat().st_size > 0:
        return target
    logger.info("downloading %s → %s", _HF_FILENAME, target)
    downloaded = hf_hub_download(
        repo_id=_HF_REPO,
        filename=_HF_FILENAME,
        local_dir=str(vad_dir),
    )
    # hf_hub_download preserves the relative path "onnx/model.onnx" inside
    # local_dir — symlink or rename to our canonical name.
    src = Path(downloaded)
    if src.resolve() != target.resolve():
        target.unlink(missing_ok=True)
        target.symlink_to(src.resolve())
    return target


class SileroVAD:
    """Stateful VAD: feed 32 ms chunks of mono float32 16 kHz audio."""

    SAMPLE_RATE: int = 16000
    SAMPLES_PER_CHUNK: int = 512  # 32 ms at 16 kHz

    def __init__(self, *, data_root: Path) -> None:
        self._model_path = _ensure_model(data_root)
        self._session = ort.InferenceSession(
            str(self._model_path),
            providers=["CPUExecutionProvider"],
        )
        # LSTM hidden state — silero v5 uses a single 'state' tensor of
        # shape (2, 1, 128).
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(self.SAMPLE_RATE, dtype=np.int64)

    def reset(self) -> None:
        """Drop carrier state — start of a new utterance / silence period."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def speech_probability(self, chunk: np.ndarray) -> float:
        """Return speech probability for one chunk in [0, 1]."""
        if chunk.shape != (self.SAMPLES_PER_CHUNK,):
            raise ValueError(
                f"chunk size must be {self.SAMPLES_PER_CHUNK} samples; got {chunk.shape}"
            )
        if chunk.dtype != np.float32:
            chunk = chunk.astype(np.float32)
        inputs = {
            "input": chunk.reshape(1, -1),
            "state": self._state,
            "sr": self._sr,
        }
        outputs = self._session.run(["output", "stateN"], inputs)
        prob = float(outputs[0].squeeze())
        self._state = outputs[1]
        return prob

    def close(self) -> None:
        self._session = None
