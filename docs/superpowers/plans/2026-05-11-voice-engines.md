# Voice Engines + Character Pack Voice Config Implementation Plan (Phase A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the foundation of the voice pipeline — ABC + registry, sherpa-onnx ASR engine (auto-download), luxtts-onnx TTS engine, character pack voice config loader, and voice prepare CLI. No daemon integration / WebRTC yet (Phase B).

**Architecture:** New `src/dollos/voice/` module with engine ABCs + decorator-based registries, plus two concrete implementations. Engines load models from `<data_root>/voice/{asr,tts}/<id>/`, auto-downloading from HuggingFace Hub on first use. A small TOML loader reads per-character `voice/engine.toml`. A CLI subcommand encodes luxtts voice-clone prompts into character packs.

**Tech Stack:** Python 3.13, sherpa-onnx (PyPI), luxtts-onnx (local path dep), huggingface_hub, numpy, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-05-11-voice-pipeline-design.md` (sections "Engine plugin model", "MVP engine choices", "Character pack voice layout", "Model lifecycle summary", "Voice prepare CLI").

**Phase placement:** This is Phase A of 3.
- **A (this plan):** Engines + pack config + prepare CLI. Engines work standalone (testable via fixtures / CLI).
- **B (next plan):** WebRTC signaling + VoiceSession + IPC integration (aiortc, sink interception).
- **C (next plan):** Local-audio-bridge process + end-to-end smoke.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | Modify | Add `sherpa-onnx`, `luxtts-onnx` (local path), `huggingface_hub` deps. |
| `src/dollos/voice/__init__.py` | Create | Module marker; re-export public API. |
| `src/dollos/voice/engines.py` | Create | `ASREngine` ABC, `TTSEngine` ABC, `ASR_REGISTRY`, `TTS_REGISTRY`, `register_asr` / `register_tts` decorators. |
| `src/dollos/voice/asr_sherpa.py` | Create | `SherpaOnnxASR` impl with model registry + auto-download. |
| `src/dollos/voice/tts_luxtts.py` | Create | `LuxTTSEngine` impl wrapping luxtts-onnx's batch `generate` into streaming PCM chunks. |
| `src/dollos/voice/pack.py` | Create | Load `voice/engine.toml` from a character pack dir; build engine instances from config. |
| `src/dollos/voice/prepare.py` | Create | CLI module `python -m dollos.voice.prepare ...` for luxtts prompt encoding. |
| `tests/voice/__init__.py` | Create | Test package marker. |
| `tests/voice/test_engines.py` | Create | Unit tests for ABC contracts + registry decorators. |
| `tests/voice/test_pack.py` | Create | Unit tests for voice config loading + engine instantiation from config. |
| `tests/voice/test_asr_sherpa.py` | Create | Integration test for SherpaOnnxASR (marked `voice_integration`, skipped by default — needs ~239 MB model download). |
| `tests/voice/test_tts_luxtts.py` | Create | Integration test for LuxTTSEngine (marked `voice_integration`). |
| `tests/voice/test_prepare.py` | Create | Smoke test for prepare CLI argument parsing + invocation flow (mocks luxtts encoder). |
| `pyproject.toml` | Modify | Register `voice_integration` pytest marker (so it's known + skipped by default). |
| `docs/roadmap.md` | Modify | Add step 26 entry (Phase A merged). |
| `CLAUDE.md` | Modify | Add roadmap entry + voice module to file map. |

---

## Task 1: Add voice engine dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Inspect current dependencies**

```bash
cat pyproject.toml | grep -A 30 "^\[project\]"
```

Note the existing dependency style (PEP 621 `dependencies = [...]` list).

- [ ] **Step 2: Add the three dependencies**

In `pyproject.toml` under `[project] dependencies = [...]`, add:

```toml
"sherpa-onnx>=1.10.0",
"luxtts-onnx",
"huggingface-hub>=0.24.0",
```

For `luxtts-onnx` (local path dep, since the project sits next to `~/Projects/luxtts-onnx`), add a `[tool.uv.sources]` override below the `[project]` block (if no `[tool.uv.sources]` section exists, create it):

```toml
[tool.uv.sources]
luxtts-onnx = { path = "../../luxtts-onnx", editable = true }
```

The relative path `../../luxtts-onnx` is from the worktree root up to `~/Projects/luxtts-onnx`.

- [ ] **Step 3: Register the voice_integration pytest marker**

Find `[tool.pytest.ini_options]` (or equivalent). Add a `markers` list if absent, append:

```toml
[tool.pytest.ini_options]
markers = [
    "voice_integration: tests that load real ONNX models (slow, downloads on first run)",
]
```

If a `markers` list exists already, just append the new entry.

- [ ] **Step 4: Sync dependencies**

```bash
uv sync
```

Expected: installs sherpa-onnx, luxtts-onnx (editable), huggingface-hub. No errors. If sherpa-onnx native wheel fails to install on this host (rare on x86_64 Linux), STOP and report — host may need a different sherpa-onnx variant (`sherpa-onnx-cpu` vs `sherpa-onnx-gpu`).

- [ ] **Step 5: Verify imports work**

```bash
uv run python -c "import sherpa_onnx; import luxtts_onnx; import huggingface_hub; print('all imports OK')"
```

Expected: `all imports OK` printed.

- [ ] **Step 6: Run full test suite to confirm no regressions**

```bash
uv run pytest -q
```

Expected: 336 passed (unchanged from main baseline).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add sherpa-onnx, luxtts-onnx, huggingface_hub for voice pipeline"
```

---

## Task 2: Engine ABC + decorator registry

**Files:**
- Create: `src/dollos/voice/__init__.py`
- Create: `src/dollos/voice/engines.py`
- Test: `tests/voice/__init__.py`, `tests/voice/test_engines.py`

- [ ] **Step 1: Write failing tests**

Create `tests/voice/__init__.py` empty.

Create `tests/voice/test_engines.py`:

```python
"""ABC + registry tests for voice engines."""
from __future__ import annotations

import pytest

from dollos.voice.engines import (
    ASR_REGISTRY,
    ASREngine,
    TTS_REGISTRY,
    TTSEngine,
    register_asr,
    register_tts,
)


def test_register_asr_adds_to_registry():
    @register_asr("fake-asr")
    class _FakeASR(ASREngine):
        async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
            return "ok"

        async def aclose(self) -> None:
            pass

    assert ASR_REGISTRY["fake-asr"] is _FakeASR
    del ASR_REGISTRY["fake-asr"]


def test_register_tts_adds_to_registry():
    @register_tts("fake-tts")
    class _FakeTTS(TTSEngine):
        sample_rate = 48000

        async def synthesize(self, text: str):
            yield b""

        async def aclose(self) -> None:
            pass

    assert TTS_REGISTRY["fake-tts"] is _FakeTTS
    del TTS_REGISTRY["fake-tts"]


def test_asr_abc_rejects_instantiation_without_methods():
    class _Bad(ASREngine):
        pass

    with pytest.raises(TypeError):
        _Bad()


def test_tts_abc_rejects_instantiation_without_methods():
    class _Bad(TTSEngine):
        pass

    with pytest.raises(TypeError):
        _Bad()


@pytest.mark.asyncio
async def test_fake_asr_transcribe_contract():
    @register_asr("contract-asr")
    class _C(ASREngine):
        async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
            assert isinstance(audio_pcm, bytes)
            assert isinstance(sample_rate, int)
            return "hello"

        async def aclose(self) -> None:
            pass

    eng = _C()
    out = await eng.transcribe(b"\x00\x00", 16000)
    assert out == "hello"
    await eng.aclose()
    del ASR_REGISTRY["contract-asr"]


@pytest.mark.asyncio
async def test_fake_tts_synthesize_yields_pcm():
    @register_tts("contract-tts")
    class _C(TTSEngine):
        sample_rate = 48000

        async def synthesize(self, text: str):
            yield b"\x00" * 100
            yield b"\x01" * 100

        async def aclose(self) -> None:
            pass

    eng = _C()
    chunks = [c async for c in eng.synthesize("hi")]
    assert chunks == [b"\x00" * 100, b"\x01" * 100]
    await eng.aclose()
    del TTS_REGISTRY["contract-tts"]
```

- [ ] **Step 2: Run tests, expect ImportError**

```bash
uv run pytest tests/voice/test_engines.py -v
```

Expected: `ModuleNotFoundError: No module named 'dollos.voice'`.

- [ ] **Step 3: Create the module**

Create `src/dollos/voice/__init__.py`:
```python
"""Voice subsystem — ASR + TTS engine plugins, character pack voice config."""
```

Create `src/dollos/voice/engines.py`:

```python
"""ASR / TTS engine ABCs + decorator-based registries.

Adding a new engine = write a class implementing the ABC, decorate with
@register_asr("<name>") or @register_tts("<name>"). The class becomes
available to character pack voice configs via that name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class ASREngine(ABC):
    """Speech recognition. Utterance-batch input → final transcript string."""

    @abstractmethod
    async def transcribe(self, audio_pcm: bytes, sample_rate: int) -> str:
        """Return the transcript for one utterance.

        audio_pcm: mono int16 little-endian PCM bytes.
        sample_rate: the engine resamples internally if it differs from
            the model's native rate.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (model handles, threads, etc.)."""


class TTSEngine(ABC):
    """Text-to-speech. Text in → streaming PCM chunks out at self.sample_rate."""

    sample_rate: int  # output sample rate in Hz; concrete classes must set this

    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mono int16 little-endian PCM chunks for the spoken text.

        Each yielded chunk is ~10-20ms of audio so callers can pipe into
        a streaming sink (WebRTC track, file writer) without buffering
        the whole utterance.
        """

    @abstractmethod
    async def aclose(self) -> None: ...


ASR_REGISTRY: dict[str, type[ASREngine]] = {}
TTS_REGISTRY: dict[str, type[TTSEngine]] = {}


def register_asr(name: str):
    """Decorator: register an ASREngine subclass under `name`."""
    def decorate(cls: type[ASREngine]) -> type[ASREngine]:
        if not issubclass(cls, ASREngine):
            raise TypeError(f"{cls.__name__} must subclass ASREngine")
        ASR_REGISTRY[name] = cls
        return cls
    return decorate


def register_tts(name: str):
    """Decorator: register a TTSEngine subclass under `name`."""
    def decorate(cls: type[TTSEngine]) -> type[TTSEngine]:
        if not issubclass(cls, TTSEngine):
            raise TypeError(f"{cls.__name__} must subclass TTSEngine")
        TTS_REGISTRY[name] = cls
        return cls
    return decorate
```

- [ ] **Step 4: Run tests, expect pass**

```bash
uv run pytest tests/voice/test_engines.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: 342 passed (336 + 6).

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/__init__.py src/dollos/voice/engines.py tests/voice/
git commit -m "feat(voice): ABC + decorator registry for ASR/TTS engines"
```

---

## Task 3: SherpaOnnxASR with model registry + auto-download

**Files:**
- Create: `src/dollos/voice/asr_sherpa.py`
- Test: `tests/voice/test_asr_sherpa.py`

- [ ] **Step 1: Write a unit test that uses fakes (does NOT load real models)**

`tests/voice/test_asr_sherpa.py`:

```python
"""SherpaOnnxASR tests.

Two layers:
- Unit tests (always run): model registry lookup, missing model handling.
- Integration tests (marked voice_integration): load a real SenseVoice
  int8 model from HuggingFace and transcribe a fixture utterance.
  Skipped by default; opt in via `pytest -m voice_integration`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.voice.asr_sherpa import SHERPA_MODELS, SherpaOnnxASR


def test_sherpa_model_registry_has_known_models():
    assert "sense-voice-zh-en-ja-ko-yue" in SHERPA_MODELS
    entry = SHERPA_MODELS["sense-voice-zh-en-ja-ko-yue"]
    assert "hf_repo" in entry
    assert "files" in entry
    assert "loader" in entry
    assert entry["loader"] in {"sense_voice", "paraformer"}


def test_sherpa_unknown_model_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown sherpa-onnx model"):
        SherpaOnnxASR(model_id="does-not-exist", data_root=tmp_path)


def test_sherpa_explicit_model_dir_skips_download(tmp_path: Path):
    """If model_dir is set and incomplete, constructor must NOT try to download."""
    custom = tmp_path / "my_models"
    custom.mkdir()
    # No model files present — construction should fail with a clear error
    # but should NOT attempt network I/O. (We can't directly assert "no
    # network", but the FileNotFoundError must surface before any HTTP.)
    with pytest.raises(FileNotFoundError, match="model file"):
        SherpaOnnxASR(
            model_id="sense-voice-zh-en-ja-ko-yue",
            data_root=tmp_path,
            model_dir=custom,
        )


@pytest.mark.voice_integration
@pytest.mark.asyncio
async def test_sherpa_transcribe_sense_voice_int8(tmp_path: Path):
    """Live integration: downloads SenseVoice (~239 MB) + transcribes a synthetic
    sine-wave-with-silence fixture. Skipped unless -m voice_integration."""
    engine = SherpaOnnxASR(
        model_id="sense-voice-zh-en-ja-ko-yue",
        data_root=tmp_path,
    )
    # 1 second of silence (silence at any rate produces empty / short transcript).
    silence = b"\x00\x00" * 16000
    out = await engine.transcribe(silence, 16000)
    assert isinstance(out, str)
    # Silence should produce empty or near-empty text — accept either.
    await engine.aclose()
```

- [ ] **Step 2: Run unit tests, expect ImportError**

```bash
uv run pytest tests/voice/test_asr_sherpa.py -v -m "not voice_integration"
```

Expected: `ImportError` on `dollos.voice.asr_sherpa`.

- [ ] **Step 3: Implement SherpaOnnxASR**

Create `src/dollos/voice/asr_sherpa.py`:

```python
"""SherpaOnnxASR — sherpa-onnx ASR engine with auto-download model registry.

Adding a new sherpa-onnx model = add an entry to SHERPA_MODELS. The
entry's `loader` field picks the right OfflineRecognizer constructor.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
import sherpa_onnx
from huggingface_hub import hf_hub_download

from dollos.voice.engines import ASREngine, register_asr

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
            repo_id=hf_repo, filename=fname, local_dir=model_dir,
        )
        # hf_hub_download already places into local_dir; check.
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
        # Strict mode when model_dir provided: must already contain files.
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
        # int16 little-endian → float32 in [-1, 1]
        samples = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text or ""

    async def aclose(self) -> None:
        # sherpa-onnx OfflineRecognizer has no explicit close; native dtor cleans up
        # when the Python object is GC'd. Setting to None drops our reference.
        self._recognizer = None
```

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest tests/voice/test_asr_sherpa.py -v -m "not voice_integration"
```

Expected: 3 passed (the 3 unit tests), 1 skipped (the voice_integration one).

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 345 passed (342 + 3 new), 1 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/asr_sherpa.py tests/voice/test_asr_sherpa.py
git commit -m "feat(voice): SherpaOnnxASR with model registry + auto-download"
```

---

## Task 4: LuxTTSEngine wrapping luxtts-onnx

**Files:**
- Create: `src/dollos/voice/tts_luxtts.py`
- Test: `tests/voice/test_tts_luxtts.py`

- [ ] **Step 1: Write tests**

`tests/voice/test_tts_luxtts.py`:

```python
"""LuxTTSEngine tests."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dollos.voice.tts_luxtts import LuxTTSEngine


def test_luxtts_sample_rate_is_48000():
    """LuxTTS output is fixed at 48 kHz."""
    assert LuxTTSEngine.sample_rate == 48000


def test_luxtts_chunk_size_is_20ms_at_48k():
    """20ms × 48000 samples/sec × 2 bytes/sample = 1920 bytes per chunk."""
    from dollos.voice.tts_luxtts import _CHUNK_BYTES
    assert _CHUNK_BYTES == 1920


def test_luxtts_pcm_chunking_matches_array(tmp_path: Path):
    """Internal helper: split float32 [N] @ 48k into 20ms int16 chunks."""
    from dollos.voice.tts_luxtts import _pcm_chunks

    audio = np.zeros(48000, dtype=np.float32)  # 1 second of silence
    audio[0] = 0.5  # one non-zero sample to verify scaling
    chunks = list(_pcm_chunks(audio))
    # 1 second @ 20ms chunks = 50 chunks
    assert len(chunks) == 50
    assert all(len(c) == 1920 for c in chunks)
    # First chunk's first int16 sample should be 0.5 * 32767 = 16383
    first_sample = int.from_bytes(chunks[0][:2], "little", signed=True)
    assert first_sample == 16383


def test_luxtts_prompt_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="prompt"):
        LuxTTSEngine(
            model_dir=tmp_path / "missing-models",
            prompt_path=tmp_path / "missing.npz",
            data_root=tmp_path,
        )


@pytest.mark.voice_integration
@pytest.mark.asyncio
async def test_luxtts_synthesize_yields_audio(tmp_path: Path):
    """Live integration: downloads luxtts models (~542 MB) and synthesizes
    a short utterance using the built-in default prompt. Skipped unless
    -m voice_integration."""
    from luxtts_onnx import LuxTTSOnnx

    # Use luxtts default models + an encoded prompt from a tiny fixture.
    # For minimal smoke, encode a 2s sine-wave reference (silly but valid).
    lt = LuxTTSOnnx(model_dir=str(tmp_path / "luxtts-models"))
    fake_wav = tmp_path / "ref.wav"
    sr = 24000
    t = np.linspace(0, 2.0, 2 * sr, dtype=np.float32)
    sine = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    import soundfile as sf  # luxtts pulls librosa which pulls soundfile
    sf.write(fake_wav, sine, sr)
    prompt = lt.encode_prompt(
        audio_path=str(fake_wav),
        transcript="A short test reference.",
        duration=2.0,
    )
    prompt_path = tmp_path / "prompt.npz"
    lt.save_prompt(prompt, str(prompt_path))

    engine = LuxTTSEngine(
        model_dir=tmp_path / "luxtts-models",
        prompt_path=prompt_path,
        data_root=tmp_path,
    )
    chunks = []
    async for chunk in engine.synthesize("Hello world."):
        chunks.append(chunk)
    assert len(chunks) > 0
    total_bytes = sum(len(c) for c in chunks)
    # At least 0.5s of audio = 0.5 * 48000 * 2 = 48000 bytes
    assert total_bytes > 48000
    await engine.aclose()
```

- [ ] **Step 2: Run unit tests**

```bash
uv run pytest tests/voice/test_tts_luxtts.py -v -m "not voice_integration"
```

Expected: ImportError on `dollos.voice.tts_luxtts`.

- [ ] **Step 3: Implement LuxTTSEngine**

Create `src/dollos/voice/tts_luxtts.py`:

```python
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
        # Pad the tail chunk to a full frame so downstream Opus encoders
        # don't choke on a short final frame.
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
    ) -> None:
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"luxtts prompt file not found: {prompt_path}; "
                f"run `python -m dollos.voice.prepare` to encode one"
            )
        # luxtts auto-downloads models on first init when model_dir is empty.
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
        )
        for chunk in _pcm_chunks(audio):
            yield chunk

    async def aclose(self) -> None:
        self._tts = None
        self._prompt = None
```

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest tests/voice/test_tts_luxtts.py -v -m "not voice_integration"
```

Expected: 4 passed, 1 skipped.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 349 passed (345 + 4), 2 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/tts_luxtts.py tests/voice/test_tts_luxtts.py
git commit -m "feat(voice): LuxTTSEngine — streaming PCM chunks at 48 kHz"
```

---

## Task 5: Character pack voice config loader

**Files:**
- Create: `src/dollos/voice/pack.py`
- Test: `tests/voice/test_pack.py`

- [ ] **Step 1: Write tests**

`tests/voice/test_pack.py`:

```python
"""Voice config loading from character packs."""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.voice.pack import (
    VoiceConfig,
    load_voice_config,
    no_voice_config,
)


def _write_pack(pack_dir: Path, *, with_voice: bool = True) -> None:
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "test"\nname = "Test"\n[identity]\nself=""\npersonality=""\ntaboos=""\n'
    )
    if with_voice:
        voice_dir = pack_dir / "voice"
        voice_dir.mkdir()
        (voice_dir / "engine.toml").write_text(
            """
[asr]
engine = "sherpa-onnx"
model_id = "sense-voice-zh-en-ja-ko-yue"
device = "cpu"

[tts]
engine = "luxtts-onnx"
prompt = "voice/luxtts/prompt.npz"
device = "cpu"
num_steps = 8
t_shift = 0.9
guidance_scale = 3.0
"""
        )


def test_load_voice_config_present(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=True)
    cfg = load_voice_config(pack_dir)
    assert isinstance(cfg, VoiceConfig)
    assert cfg.asr is not None
    assert cfg.asr["engine"] == "sherpa-onnx"
    assert cfg.asr["model_id"] == "sense-voice-zh-en-ja-ko-yue"
    assert cfg.tts is not None
    assert cfg.tts["engine"] == "luxtts-onnx"
    # Relative prompt path resolved against pack_dir
    assert cfg.tts["prompt"] == pack_dir / "voice/luxtts/prompt.npz"


def test_load_voice_config_absent_returns_no_voice(tmp_path: Path):
    pack_dir = tmp_path / "pack"
    _write_pack(pack_dir, with_voice=False)
    cfg = load_voice_config(pack_dir)
    assert cfg == no_voice_config()
    assert cfg.asr is None
    assert cfg.tts is None


def test_load_voice_config_missing_asr_section_ok(tmp_path: Path):
    """A pack can have TTS without ASR (or vice versa)."""
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "test"\nname = "Test"\n[identity]\nself=""\npersonality=""\ntaboos=""\n'
    )
    (pack_dir / "voice").mkdir()
    (pack_dir / "voice" / "engine.toml").write_text(
        """
[tts]
engine = "luxtts-onnx"
prompt = "voice/luxtts/prompt.npz"
"""
    )
    cfg = load_voice_config(pack_dir)
    assert cfg.asr is None
    assert cfg.tts is not None
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/voice/test_pack.py -v
```

- [ ] **Step 3: Implement pack.py**

Create `src/dollos/voice/pack.py`:

```python
"""Load voice/engine.toml from a character pack."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceConfig:
    """Per-character voice configuration. asr / tts are None when absent."""

    asr: dict | None = None  # raw fields from [asr] section; relative paths resolved
    tts: dict | None = None  # raw fields from [tts] section; relative paths resolved


def no_voice_config() -> VoiceConfig:
    return VoiceConfig()


def _resolve_path_fields(section: dict, pack_dir: Path) -> dict:
    """Resolve relative path-like fields against pack_dir.

    Recognized path keys: `model_dir`, `prompt`. Absolute paths are kept.
    """
    out = dict(section)
    for key in ("model_dir", "prompt"):
        if key in out and isinstance(out[key], str):
            p = Path(out[key])
            out[key] = p if p.is_absolute() else (pack_dir / p)
    return out


def load_voice_config(pack_dir: Path) -> VoiceConfig:
    """Read pack_dir/voice/engine.toml. Returns empty config if file absent.

    Relative `model_dir` / `prompt` paths are resolved against pack_dir.
    """
    engine_toml = pack_dir / "voice" / "engine.toml"
    if not engine_toml.exists():
        return no_voice_config()
    with engine_toml.open("rb") as f:
        raw = tomllib.load(f)
    asr = _resolve_path_fields(raw["asr"], pack_dir) if "asr" in raw else None
    tts = _resolve_path_fields(raw["tts"], pack_dir) if "tts" in raw else None
    return VoiceConfig(asr=asr, tts=tts)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_pack.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 352 passed (349 + 3), 2 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/pack.py tests/voice/test_pack.py
git commit -m "feat(voice): character pack voice/engine.toml loader"
```

---

## Task 6: Voice prepare CLI for luxtts prompt encoding

**Files:**
- Create: `src/dollos/voice/prepare.py`
- Test: `tests/voice/test_prepare.py`

- [ ] **Step 1: Write smoke tests**

`tests/voice/test_prepare.py`:

```python
"""Smoke test the voice prepare CLI flow with luxtts mocked."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_prepare_argv_parsing():
    from dollos.voice.prepare import build_parser
    p = build_parser()
    args = p.parse_args([
        "--pack", "character_packs/gura",
        "--ref", "ref.wav",
        "--transcript", "hi",
        "--duration", "12.5",
    ])
    assert args.pack == Path("character_packs/gura")
    assert args.ref == Path("ref.wav")
    assert args.transcript == "hi"
    assert args.duration == 12.5


def test_prepare_writes_prompt_npz(tmp_path: Path, monkeypatch):
    from dollos.voice import prepare
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    ref_wav = tmp_path / "ref.wav"
    ref_wav.write_bytes(b"\x00" * 100)  # dummy

    # Mock LuxTTSOnnx so we don't trigger model download.
    fake_tts = MagicMock()
    fake_tts.encode_prompt.return_value = {"fake": "prompt"}
    fake_tts.save_prompt = MagicMock()

    monkeypatch.setattr(prepare, "LuxTTSOnnx", lambda **kw: fake_tts)

    out_path = prepare.run_prepare(
        pack=pack_dir,
        ref=ref_wav,
        transcript="hello",
        duration=10.0,
        data_root=tmp_path / "data",
    )
    expected = pack_dir / "voice" / "luxtts" / "prompt.npz"
    assert out_path == expected
    assert (pack_dir / "voice" / "luxtts").exists()
    fake_tts.encode_prompt.assert_called_once_with(
        audio_path=str(ref_wav), transcript="hello", duration=10.0,
    )
    fake_tts.save_prompt.assert_called_once_with(
        {"fake": "prompt"}, str(expected),
    )
    # ref.meta.toml recorded
    meta = (pack_dir / "voice" / "luxtts" / "ref.meta.toml").read_text()
    assert "hello" in meta
    assert "ref.wav" in meta
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/voice/test_prepare.py -v
```

- [ ] **Step 3: Implement prepare.py**

Create `src/dollos/voice/prepare.py`:

```python
"""voice prepare CLI: encode a luxtts voice-clone prompt into a character pack.

Usage:
    python -m dollos.voice.prepare \\
        --pack character_packs/gura \\
        --ref reference_recording.wav \\
        --transcript "Reference transcript." \\
        --duration 15.0
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from luxtts_onnx import LuxTTSOnnx


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.voice.prepare",
        description="Encode a luxtts voice-clone prompt for a character pack",
    )
    p.add_argument("--pack", type=Path, required=True,
                   help="character pack directory")
    p.add_argument("--ref", type=Path, required=True,
                   help="reference audio wav file")
    p.add_argument("--transcript", type=str, required=True,
                   help="exact transcript of the reference audio")
    p.add_argument("--duration", type=float, default=15.0,
                   help="reference duration in seconds (default 15)")
    p.add_argument("--data-root", type=Path, default=Path("data"),
                   help="daemon data root (where models cache lives; default ./data)")
    return p


def run_prepare(
    *,
    pack: Path,
    ref: Path,
    transcript: str,
    duration: float,
    data_root: Path,
) -> Path:
    """Encode prompt + write into the pack. Returns the prompt.npz path."""
    if not pack.exists():
        raise FileNotFoundError(f"pack dir not found: {pack}")
    if not ref.exists():
        raise FileNotFoundError(f"reference audio not found: {ref}")

    model_dir = data_root / "voice" / "tts" / "luxtts"
    model_dir.mkdir(parents=True, exist_ok=True)

    tts = LuxTTSOnnx(model_dir=str(model_dir), provider="cpu")
    prompt = tts.encode_prompt(
        audio_path=str(ref), transcript=transcript, duration=duration,
    )
    out_dir = pack / "voice" / "luxtts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prompt.npz"
    tts.save_prompt(prompt, str(out_path))

    # Record provenance.
    meta = (
        f'# voice-clone prompt provenance\n'
        f'ref_path = "{ref}"\n'
        f'transcript = """{transcript}"""\n'
        f'duration_s = {duration}\n'
        f'encoded_at = "{datetime.now().isoformat(timespec="seconds")}"\n'
    )
    (out_dir / "ref.meta.toml").write_text(meta)
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = run_prepare(
        pack=args.pack,
        ref=args.ref,
        transcript=args.transcript,
        duration=args.duration,
        data_root=args.data_root,
    )
    print(f"wrote prompt to: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/voice/test_prepare.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Full suite**

```bash
uv run pytest -q -m "not voice_integration"
```

Expected: 354 passed (352 + 2), 2 skipped.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/prepare.py tests/voice/test_prepare.py
git commit -m "feat(voice): prepare CLI for luxtts voice-clone prompt encoding"
```

---

## Task 7: Update docs (roadmap + CLAUDE.md)

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: roadmap.md — add step 26 entry**

Insert above the step 25 entry:

```markdown
### 26. Voice engines + character pack voice config (Phase A of voice pipeline)  ✅ Merged

**範圍**：
- 新 `src/dollos/voice/` 模組：ABC + decorator registry、SherpaOnnxASR、LuxTTSEngine、character pack `voice/engine.toml` 讀取、voice prepare CLI
- Engines auto-download：sherpa-onnx 拉 SenseVoice int8 (~239 MB) 到 `data/voice/asr/`，luxtts-onnx 拉它的 onnx 檔 (~542 MB) 到 `data/voice/tts/luxtts/`
- Voice clone prompt 留在 character pack（`character_packs/<id>/voice/luxtts/prompt.npz`），用 `python -m dollos.voice.prepare` 一次性編好
- 暫時還沒接 daemon — engines + config 都 standalone testable

**設計選擇**：
- ABC + registry：新 engine = 一個 file + 一個 decorator，不改核心
- ASR 用 sherpa-onnx（torch-free，多語 SenseVoice 支援好）；TTS 用 luxtts-onnx（torch-free，voice clone）
- 模型住 `data/voice/...`，跟 memory / schedule 同根；voice clone prompt 住 character pack（角色身份）
- Voice config 用獨立 `voice/engine.toml`，跟 `doll.toml` 分開（engine 細節跟角色身份分層）

**Tests**：~354 passed（18 new voice unit tests + 2 voice_integration markers，後者預設 skip）

**Phase 後續**：
- B（next plan）：WebRTC + VoiceSession + IPC integration
- C（next plan）：local-audio-bridge + E2E
```

- [ ] **Step 2: CLAUDE.md — update plans table + file map**

Add to the "已完成" Roadmap table:
```
| Roadmap step 26 — Voice engines + pack config (Voice Phase A) | Merged |
```

In "下一個":
```
- **Voice pipeline Phase B**：WebRTC + VoiceSession + IPC integration
- **Voice pipeline Phase C**：local-audio-bridge + E2E
- **Drone**（persistent agents — 跟 Subagent 對偶；Monitor 是無大腦版，Drone 是有大腦版）
- **Wake gating** — 等 voice / drone events 進來才有 ROI
```

In "Future structure of this repo" diagram (if present), add:
```
├── src/dollos/voice/             # ASR/TTS engines + pack config
```

- [ ] **Step 3: Commit**

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "docs: roadmap step 26 — voice engines (Phase A)"
```

---

## Self-Review Checklist

- [x] **Spec coverage**:
  - Engine ABC + registry → Task 2
  - SherpaOnnxASR + model registry + auto-download → Task 3
  - LuxTTSEngine + chunking → Task 4
  - Character pack voice config layout + loader → Task 5
  - Voice prepare CLI → Task 6
  - Model lifecycle (data/voice/...) → Tasks 3, 4, 6
  - Acceptance criterion "engine registry exposed, adding new engine = single file" → Task 2 (decorator) ✓
- [x] **No placeholders**: all code blocks complete; bash commands concrete with expected output.
- [x] **Type consistency**:
  - `ASREngine.transcribe(audio_pcm: bytes, sample_rate: int) -> str` consistent across ABC, test, impl
  - `TTSEngine.synthesize(text: str) -> AsyncIterator[bytes]` consistent
  - `TTSEngine.sample_rate` class attribute → `LuxTTSEngine.sample_rate = 48000` ✓
  - `SherpaOnnxASR.__init__(*, model_id, data_root, model_dir=None, device, num_threads)` consistent in test + impl
  - `LuxTTSEngine.__init__(*, model_dir, prompt_path, data_root, device, num_steps, t_shift, guidance_scale)` consistent
  - `VoiceConfig.asr / .tts` are `dict | None` — consistent with test assertions
  - `_pcm_chunks` returns `Iterator[bytes]` (sync iter, consumed by async wrapper) — tests verify chunk sizes match `_CHUNK_BYTES`
- [x] **Test isolation**: voice_integration marker keeps slow + network-dependent tests opt-in
- [x] **No fallback**: missing model file raises FileNotFoundError; unknown model raises ValueError. No silent degrade.

## Notes for Reviewer

- **Phase A is intentionally not daemon-integrated**: engines work in isolation. Phase B adds the IPC wiring. This means after Phase A, voice features are NOT user-visible end-to-end — that's expected.
- **soundfile dependency**: luxtts pulls librosa pulls soundfile; available in the smoke test. If on a host without libsndfile, the voice_integration luxtts test will fail at import. Document in README.
- **sherpa-onnx OfflineRecognizer API**: confirmed `from_sense_voice` and `from_paraformer` class methods exist in sherpa-onnx >= 1.10. If a future version renames, fix in `_transcribe_sync`.
- **luxtts auto-download**: triggers in `LuxTTSOnnx.__init__` when `model_dir` is empty. To prepopulate without instantiating the full engine, future task could add a `--prefetch` flag to prepare CLI.

## Out of scope (next plans)

- WebRTC peer connection, VoiceSession orchestration, SDP/ICE signaling (Phase B)
- IPC TextChunk → TTS interceptor (Phase B)
- Kernel integration + per-WS voice session lifecycle (Phase B)
- local-audio-bridge client process (Phase C)
- End-to-end live smoke (Phase C)
