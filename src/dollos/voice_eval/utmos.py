"""UTMOSv2 — predicted MOS for speech naturalness.

UTMOSv2 (Saeki et al., Interspeech 2024) is a self-supervised MOS
predictor. Repo: https://github.com/sarulab-speech/UTMOSv2

Install:
    uv pip install git+https://github.com/sarulab-speech/UTMOSv2.git

If utmosv2 is not installed, `_ensure_loaded` raises RuntimeError; the
voice-eval CLI catches this and marks the row as skipped.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _pick_device() -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda:0" if torch.cuda.is_available() else "cpu"


class UTMOSRunner:
    """Lazy-load UTMOSv2 once; predict MOS for arbitrary wavs."""

    def __init__(self) -> None:
        self._model = None
        self._device: str = "cpu"

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import utmosv2  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "UTMOSv2 not available; install with: "
                "uv pip install git+https://github.com/sarulab-speech/UTMOSv2.git"
            ) from e
        if utmosv2 is None:
            raise RuntimeError(
                "UTMOSv2 not available; install with: "
                "uv pip install git+https://github.com/sarulab-speech/UTMOSv2.git"
            )
        self._device = _pick_device()
        logger.info("loading UTMOSv2 on %s ...", self._device)
        try:
            self._model = utmosv2.create_model(pretrained=True, device=self._device)
        except AttributeError as e:
            raise RuntimeError("UTMOSv2 API not as expected; see repo README") from e

    def score(self, wav_path: Path) -> float:
        self._ensure_loaded()
        assert self._model is not None
        result = self._model.predict(
            input_path=str(wav_path),
            device=self._device,
            verbose=False,
        )
        return float(result)
