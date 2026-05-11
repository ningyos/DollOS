"""SherpaOnnxASR — sherpa-onnx ASR engine with auto-download model registry.

Adding a new sherpa-onnx model = add an entry to SHERPA_MODELS. The
entry's `loader` field picks the right OfflineRecognizer constructor.

Includes a self-healing bootstrap that creates the missing
libonnxruntime.so symlink in sherpa_onnx/lib/ — sherpa-onnx 1.13.1's
wheel ships only the versioned .so but its RPATH searches for the
unversioned name.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any


def _ensure_sherpa_onnx_symlink() -> None:
    """Self-heal sherpa-onnx 1.13.1's missing libonnxruntime.so dependency.

    sherpa-onnx's _sherpa_onnx.so links libonnxruntime.so (unversioned)
    via RPATH=$ORIGIN, but the wheel does not always ship the lib in
    sherpa_onnx/lib/. The Python onnxruntime package always has
    libonnxruntime.so.X.Y.Z in its capi/ directory; symlink it across.

    Idempotent — early-returns when the symlink already exists.
    """
    import importlib.util

    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or spec.origin is None:
        return
    lib_dir = Path(spec.origin).parent / "lib"
    target = lib_dir / "libonnxruntime.so"
    if target.exists():
        return

    # Source 1: sherpa_onnx/lib/libonnxruntime.so.X.Y.Z
    versioned = next(lib_dir.glob("libonnxruntime.so.*"), None)

    # Source 2: onnxruntime/capi/libonnxruntime.so.X.Y.Z (sibling package)
    if versioned is None:
        ort_spec = importlib.util.find_spec("onnxruntime")
        if ort_spec is not None and ort_spec.origin is not None:
            ort_capi = Path(ort_spec.origin).parent / "capi"
            versioned = next(ort_capi.glob("libonnxruntime.so.*"), None)

    if versioned is None:
        return
    try:
        # Absolute path so it works even when the file lives in a sibling package.
        target.symlink_to(versioned.resolve())
    except OSError:
        pass  # race / permission denied — let import fail naturally


_ensure_sherpa_onnx_symlink()

import numpy as np  # noqa: E402
import sherpa_onnx  # noqa: E402 — must follow bootstrap
from huggingface_hub import hf_hub_download  # noqa: E402

from dollos.voice.engines import ASREngine, register_asr  # noqa: E402

logger = logging.getLogger(__name__)


SHERPA_MODELS: dict[str, dict[str, Any]] = {
    "sense-voice-zh-en-ja-ko-yue": {
        "hf_repo": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "files": ["model.int8.onnx", "tokens.txt"],
        "loader": "sense_voice",
        "model_filename": "model.int8.onnx",
    },
    "paraformer-zh": {
        "hf_repo": "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14",
        "files": ["model.int8.onnx", "tokens.txt"],
        "loader": "paraformer",
        "model_filename": "model.int8.onnx",
    },
}


def _download_if_missing(model_dir: Path, hf_repo: str, files: list[str]) -> None:
    """Pull missing files from HuggingFace into model_dir."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for fname in files:
        dst = model_dir / fname
        if dst.exists() and dst.stat().st_size > 0:
            continue
        logger.info("downloading %s/%s → %s", hf_repo, fname, dst)
        downloaded = hf_hub_download(
            repo_id=hf_repo, filename=fname, local_dir=str(model_dir),
        )
        if not Path(downloaded).exists():
            raise RuntimeError(f"hf_hub_download failed for {hf_repo}/{fname}")


@register_asr("sherpa-onnx")
class SherpaOnnxASR(ASREngine):
    """ASR via sherpa-onnx. Auto-downloads model into data_root on first use."""

    def __init__(
        self,
        *,
        model_id: str,
        data_root: Path,
        model_dir: Path | None = None,
        device: str = "cpu",
        num_threads: int = 2,
    ) -> None:
        if model_id not in SHERPA_MODELS:
            available = ", ".join(SHERPA_MODELS.keys())
            raise ValueError(
                f"unknown sherpa-onnx model {model_id!r}; "
                f"available: {available}"
            )
        spec = SHERPA_MODELS[model_id]
        self._model_id = model_id
        self._device = device
        self._num_threads = num_threads

        if model_dir is None:
            model_dir = data_root / "voice" / "asr" / model_id
            _download_if_missing(model_dir, spec["hf_repo"], spec["files"])
        for fname in spec["files"]:
            if not (model_dir / fname).exists():
                raise FileNotFoundError(
                    f"model file {fname} not found in {model_dir}"
                )

        model_path = str(model_dir / spec["model_filename"])
        tokens_path = str(model_dir / "tokens.txt")
        provider = "cuda" if device == "cuda" else "cpu"

        if spec["loader"] == "sense_voice":
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_path,
                tokens=tokens_path,
                num_threads=num_threads,
                provider=provider,
                use_itn=True,
            )
        elif spec["loader"] == "paraformer":
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=model_path,
                tokens=tokens_path,
                num_threads=num_threads,
                provider=provider,
            )
        else:
            raise ValueError(f"unsupported loader {spec['loader']!r}")

    async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
        return await asyncio.to_thread(
            self._transcribe_sync, audio_pcm, sample_rate
        )

    def _transcribe_sync(self, audio_pcm: bytes, sample_rate: int) -> str:
        samples = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text or ""

    async def aclose(self) -> None:
        self._recognizer = None
