"""NISQA — perceived audio quality. Range 1-5.

Best-effort runner: NISQA is not pip-installable (the GitHub repo has no
pyproject.toml / setup.py; no PyPI package under nisqa-tts / nisqa-toolkit).
_ensure_loaded raises RuntimeError so the aggregator marks the row skipped.

To enable later, drop the upstream NISQA repo into vendor/ and import its
model loader directly, then adapt _ensure_loaded + score below.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class NISQARunner:
    def __init__(self) -> None:
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import nisqa  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "NISQA not available. Tried: nisqa-tts (not on PyPI), "
                "nisqa-toolkit (not on PyPI), "
                "git+https://github.com/gabrielmittag/NISQA.git "
                "(no pyproject.toml / setup.py). "
                "Leaving runner unavailable; drop the repo into vendor/ to enable."
            ) from e
        # ADAPT to actual API once a working source is available.
        self._model = nisqa.load_model()  # type: ignore[attr-defined]

    def score(self, wav_path: Path) -> float:
        self._ensure_loaded()
        # ADAPT: real API might differ
        return float(self._model.predict(str(wav_path)))  # type: ignore[union-attr]
