"""Qwen3TTSEngine — TTS via Qwen3-TTS (Alibaba) in-process.

Two cloning paths:
  1) ref_audio + ref_text — run create_voice_clone_prompt at synth time.
  2) voice_clone_prompt_path — load a pre-built .pt (built by the
     "Voice Design then Clone" workflow); skips ref_audio/ref_text entirely.
"""
from __future__ import annotations

import asyncio
import json
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


def _load_eq_curve(path: Path, engine_sample_rate: int, n_taps: int = 511):
    """Load JSON EQ curve and return ``(fir_coeffs, peak_normalize)``.

    Schema::

        {
          "name": "...",
          "sample_rate": 24000,
          "bands": [{"freq_hz": 93, "gain_db": -0.10}, ...],
          "peak_normalize": 0.95
        }

    Designs a linear-phase FIR via ``scipy.signal.firwin2`` matching the band
    gains.  Raises if the JSON sample_rate doesn't match the engine.
    """
    import numpy as np
    from scipy.signal import firwin2

    payload = json.loads(Path(path).read_text())
    sr = int(payload["sample_rate"])
    if sr != engine_sample_rate:
        raise ValueError(
            f"EQ curve sample_rate={sr} mismatches engine sample_rate="
            f"{engine_sample_rate} (path={path})"
        )
    bands = payload["bands"]
    if not bands:
        raise ValueError(f"EQ curve has no bands: {path}")
    peak_normalize = float(payload.get("peak_normalize", 0.95))

    freqs_hz = [float(b["freq_hz"]) for b in bands]
    gains_db = [float(b["gain_db"]) for b in bands]
    nyq = sr / 2.0
    # firwin2 needs freqs strictly increasing, starting at 0 ending at nyq.
    pts: list[tuple[float, float]] = [(0.0, gains_db[0])]
    last = 0.0
    for f, g in zip(freqs_hz, gains_db):
        f_clamped = min(max(f, last + 1.0), nyq - 1.0)
        if f_clamped <= last:
            continue
        pts.append((f_clamped, g))
        last = f_clamped
    pts.append((nyq, gains_db[-1]))

    freqs_norm = np.array([p[0] / nyq for p in pts], dtype=np.float64)
    gains_lin = np.array([10.0 ** (p[1] / 20.0) for p in pts], dtype=np.float64)
    # firwin2 requires odd numtaps for type-I linear phase across full band.
    if n_taps % 2 == 0:
        n_taps += 1
    fir = firwin2(n_taps, freqs_norm, gains_lin)
    return fir.astype(np.float32), peak_normalize


def _load_voice_clone_prompt(path: Path):
    """Load a .pt produced by build_powdur_voice_prompt.py / qwen-tts demo."""
    import torch
    from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

    payload = torch.load(str(path), map_location="cpu", weights_only=True)
    if not isinstance(payload, dict) or "items" not in payload:
        raise ValueError(
            f"voice_clone_prompt file has invalid format: {path}"
        )
    items_raw = payload["items"]
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError(f"voice_clone_prompt file has no items: {path}")
    items = []
    for d in items_raw:
        ref_code = d.get("ref_code", None)
        if ref_code is not None and not torch.is_tensor(ref_code):
            ref_code = torch.tensor(ref_code)
        ref_spk = d.get("ref_spk_embedding", None)
        if ref_spk is None:
            raise ValueError(f"item missing ref_spk_embedding in {path}")
        if not torch.is_tensor(ref_spk):
            ref_spk = torch.tensor(ref_spk)
        items.append(
            VoiceClonePromptItem(
                ref_code=ref_code,
                ref_spk_embedding=ref_spk,
                x_vector_only_mode=bool(d.get("x_vector_only_mode", False)),
                icl_mode=bool(d.get("icl_mode", not bool(d.get("x_vector_only_mode", False)))),
                ref_text=d.get("ref_text", None),
            )
        )
    return items


@register_tts("qwen3-tts")
class Qwen3TTSEngine(TTSEngine):
    """TTS engine wrapping Qwen3-TTS with voice cloning + emotion instruction."""

    def __init__(
        self,
        *,
        model_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device: str = "cuda:0",
        ref_audio: Path | str | None = None,
        ref_text: str | None = None,
        voice_clone_prompt_path: Path | str | None = None,
        language: str = "English",
        instruction: str = "",
        peak_target: float = 0.95,
        eq_curve_path: Path | str | None = None,
    ) -> None:
        self._peak_target = float(peak_target)
        self._eq_fir = None
        self._eq_peak_normalize: float | None = None
        self._eq_curve_path = (
            Path(eq_curve_path) if eq_curve_path is not None else None
        )
        has_prompt = voice_clone_prompt_path is not None
        has_ref = ref_audio is not None or ref_text is not None
        if has_prompt and has_ref:
            raise ValueError(
                "Qwen3TTSEngine: provide exactly one of voice_clone_prompt_path "
                "or (ref_audio + ref_text), not both."
            )
        if not has_prompt and not has_ref:
            raise ValueError(
                "Qwen3TTSEngine: must provide either voice_clone_prompt_path "
                "or (ref_audio + ref_text)."
            )

        self._model = _get_model(model_id, device)
        self._language = language
        self._instruction = instruction
        self._voice_clone_prompt = None
        self._ref_audio: str | None = None
        self._ref_text: str | None = None

        if has_prompt:
            p = Path(voice_clone_prompt_path)
            if not p.exists():
                raise FileNotFoundError(
                    f"Qwen3-TTS voice_clone_prompt_path not found: {p}"
                )
            self._voice_clone_prompt = _load_voice_clone_prompt(p)
        else:
            if ref_audio is None or ref_text is None:
                raise ValueError(
                    "Qwen3TTSEngine: ref_audio and ref_text must both be set."
                )
            if not Path(ref_audio).exists():
                raise FileNotFoundError(f"Qwen3-TTS ref_audio not found: {ref_audio}")
            self._ref_audio = str(ref_audio)
            self._ref_text = ref_text

        # qwen-tts default codec rate; re-set dynamically after first synth.
        self.sample_rate = 24000

        if self._eq_curve_path is not None:
            if not self._eq_curve_path.exists():
                raise FileNotFoundError(
                    f"Qwen3-TTS eq_curve_path not found: {self._eq_curve_path}"
                )
            fir, eq_peak = _load_eq_curve(self._eq_curve_path, self.sample_rate)
            self._eq_fir = fir
            self._eq_peak_normalize = eq_peak
            logger.info(
                "Qwen3-TTS EQ loaded from %s (%d taps, peak_normalize=%.3f)",
                self._eq_curve_path, len(fir), eq_peak,
            )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        prefixed_text = (
            f"{self._instruction}. {text}" if self._instruction else text
        )

        def _generate():
            if self._voice_clone_prompt is not None:
                return self._model.generate_voice_clone(
                    text=text,
                    language=self._language,
                    voice_clone_prompt=self._voice_clone_prompt,
                )
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
        new_sr = int(sr)
        if self._eq_fir is not None and new_sr != self.sample_rate:
            raise RuntimeError(
                f"Qwen3-TTS runtime sample_rate={new_sr} differs from EQ-designed "
                f"sample_rate={self.sample_rate}; EQ curve is invalid."
            )
        self.sample_rate = new_sr
        import numpy as np
        wave = wavs[0] if hasattr(wavs, "__getitem__") else wavs
        if hasattr(wave, "astype"):
            if np.issubdtype(wave.dtype, np.floating):
                w = np.asarray(wave, dtype=np.float32)
                if self._eq_fir is not None:
                    # Apply spectrum-match FIR EQ, then EQ-defined peak normalize.
                    from scipy.signal import lfilter
                    w = lfilter(self._eq_fir, 1.0, w).astype(np.float32)
                    peak = float(np.max(np.abs(w))) if w.size else 0.0
                    target = self._eq_peak_normalize or self._peak_target
                    if peak > 1e-6:
                        w = w * (target / peak)
                else:
                    # Peak-normalize to target before int16 to give consistent loudness;
                    # Qwen3-TTS output peak is typically ~0.3, much quieter than other engines.
                    peak = float(np.max(np.abs(w))) if w.size else 0.0
                    if peak > 1e-6:
                        w = w * (self._peak_target / peak)
                w = np.clip(w, -1.0, 1.0)
                pcm = (w * 32767.0).astype(np.int16, copy=False).tobytes()
            else:
                pcm = wave.astype(np.int16, copy=False).tobytes()
        else:
            pcm = bytes(wave)
        for chunk in _rechunk_int16(pcm, self.sample_rate):
            yield chunk

    async def aclose(self) -> None:
        pass
