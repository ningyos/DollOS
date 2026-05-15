# Qwen3-TTS Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Add Qwen3-TTS (Alibaba, Apache-2.0) as DollOS high-tier TTS backend. In-process Python integration mirroring the existing `FishTTSEngine` pattern. Powdur character pack switches to Qwen3-TTS for expressive emotion.

**Why Qwen3-TTS over S2-Pro:**
- 1.7 B parameters (~3.5 GB bf16 VRAM, fits 16 GB GPU 0 with llama-server present)
- Day-0 vLLM-Omni support upstream — no dependency-hell-via-docker
- Apache-2.0 (vs S2-Pro's Fish Audio Research License non-commercial pin)
- 3-second voice clone with explicit `ref_audio` + `ref_text` API
- Natural-language emotion instructions (free-form, no `[tag]` dialect to memorise)
- Same backbone family (Qwen3) as DollOS's Inner Voice — single tokenizer/runtime family

**Architecture:** Same in-process singleton pattern as `FishTTSEngine`: lazy-import `qwen_tts` on first construct, share `Qwen3TTSModel` across characters, swap reference profile per character via the model's API. No HTTP server, no docker, no extra process management.

**Tech Stack:** Python 3.11+, `qwen-tts` PyPI package (Apache-2.0), torch + CUDA (already required by fish-tts extra), soundfile.

---

## Pre-Plan: cleanup s2-pro deliverables

The previous abandoned attempt is on its own branch `s2pro-integration` (now defunct). All of its artifacts are in **that** branch only; `main` is clean. Nothing to delete in this branch.

To garbage-collect the s2-pro branch + container after this plan is approved:

```bash
# On main, after this plan merges:
docker rmi dollos-sglang-omni:s2pro || true
docker compose -f /tmp/nonexistent down 2>/dev/null || true   # just in case
git branch -D s2pro-integration
git worktree prune
```

---

## File Structure

| File | Responsibility |
|---|---|
| `src/dollos/voice/tts_qwen3.py` | `Qwen3TTSEngine` — wraps `qwen_tts.Qwen3TTSModel` singleton + per-character reference. Streams via `synthesize_stream` if `qwen-tts` exposes one, otherwise wraps `generate_voice_clone` and chunks the resulting full waveform into 20 ms frames. |
| `src/dollos/voice/__init__.py` | Import `tts_qwen3` so `@register_tts("qwen3-tts")` fires at package load. |
| `tests/voice/test_tts_qwen3.py` | Mock `qwen_tts` module via `monkeypatch.setitem(sys.modules, "qwen_tts", fake)`. Verify singleton load, voice-clone call args, chunking, aclose. |
| `pyproject.toml` `[qwen3]` extra | `qwen-tts>=0.1` plus a torch range matching fish-tts (torch <2.12 already pinned for fish; reuse). |
| `character_packs/_examples/voice/engine.qwen3.toml` | Example config (1.7B-Base, voice clone, emotion via `instruction` text). |
| `character_packs/powdur/voice/engine.qwen3.toml` | Powdur-specific config; sits alongside `engine.toml` (fish-tts) and `engine.s2pro.toml` (defunct, removed) so user can swap by rename. |
| `scripts/smoke_qwen3_powdur.py` | End-to-end smoke: load model, synth 3 sample sentences, write wav files, compare F0/RMS/centroid against the YouTube ref clip we already have. |

---

## Task 1 — `Qwen3TTSEngine` and tests

**Files:**
- Create: `src/dollos/voice/tts_qwen3.py`
- Create: `tests/voice/test_tts_qwen3.py`
- Modify: `src/dollos/voice/__init__.py`
- Modify: `pyproject.toml`
- Create: `character_packs/_examples/voice/engine.qwen3.toml`

### Step 1 — tests first

`tests/voice/test_tts_qwen3.py`:
```python
"""Qwen3TTSEngine tests with mocked qwen_tts package."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pytest


@pytest.fixture
def fake_qwen_tts(monkeypatch):
    fake_module = MagicMock()

    # Simulated waveform: 1 second of int16 silence at 24 kHz, returned as
    # (numpy_array_shape_(1,N), sample_rate).
    fake_wave = (np.zeros((1, 24000), dtype=np.int16),)
    fake_sr = 24000

    fake_model = MagicMock()
    fake_model.generate_voice_clone.return_value = (fake_wave, fake_sr)
    fake_module.Qwen3TTSModel.from_pretrained.return_value = fake_model

    monkeypatch.setitem(sys.modules, "qwen_tts", fake_module)
    return fake_module, fake_model


async def test_qwen3_loads_model_and_holds_reference(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="excited",
    )
    fake_module, fake_model = fake_qwen_tts
    fake_module.Qwen3TTSModel.from_pretrained.assert_called_once()
    assert eng.sample_rate == 24000


async def test_qwen3_synthesize_yields_pcm_chunks(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
    )
    chunks = [c async for c in eng.synthesize("hello world")]
    assert chunks, "engine produced no chunks"
    # 20 ms at 24 kHz int16 = 480 samples = 960 bytes
    assert all(len(c) == 960 for c in chunks[:-1])
    # Total samples should match the fake 1s waveform
    assert sum(len(c) for c in chunks) >= 24000 * 2


async def test_qwen3_passes_instruction_prefix(fake_qwen_tts, tmp_path):
    """Engine prepends instruction to text input."""
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="excited, energetic",
    )
    async for _ in eng.synthesize("hello"):
        pass
    _, fake_model = fake_qwen_tts
    call = fake_model.generate_voice_clone.call_args
    # Implementation choice: either pass instruction as a separate kwarg if
    # supported, or prepend it to text. Test verifies one or the other.
    text_arg = call.kwargs.get("text") or call.args[0]
    has_instr_in_text = "excited, energetic" in text_arg
    has_instr_kw = call.kwargs.get("instruction") == "excited, energetic"
    assert has_instr_in_text or has_instr_kw, (
        f"instruction not passed; call.kwargs={call.kwargs}"
    )


async def test_qwen3_aclose(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref, ref_text="hi", language="English",
    )
    await eng.aclose()
```

Run `uv run pytest tests/voice/test_tts_qwen3.py -v` → expect ImportError on `dollos.voice.tts_qwen3`.

### Step 2 — implementation

`src/dollos/voice/tts_qwen3.py`:
```python
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
```

### Step 3 — register in `__init__.py`

Add `from dollos.voice import tts_qwen3  # noqa: F401`.

### Step 4 — pyproject extra

```toml
qwen3 = [
    "qwen-tts>=0.1",
    # Reuse the fish stack's torch pin to keep one consistent CUDA env.
    "torch>=2.10,<2.12",
    "torchaudio>=2.10,<2.12",
]
```

### Step 5 — example config

`character_packs/_examples/voice/engine.qwen3.toml`:
```toml
[asr]
engine = "sherpa-onnx"
model_id = "sense-voice-zh-en-ja-ko-yue"
device = "cpu"

[tts]
engine = "qwen3-tts"
model_id = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
device = "cuda:0"
ref_audio = "voice/qwen3/ref.wav"
ref_text = "REFERENCE_TRANSCRIPT"
language = "English"
# Free-form natural-language voice direction. Examples:
#   "excited, energetic, animated"
#   "speak in a soft, conspiratorial whisper"
#   "用激動的語氣說"
instruction = "excited, energetic, animated"
```

### Step 6 — verify tests + commit

```bash
uv run pytest tests/voice/test_tts_qwen3.py -v   # expect 4 pass
git -c user.email=progcat@protonmail.com -c user.name=ProgCat \
    add src/dollos/voice/tts_qwen3.py src/dollos/voice/__init__.py \
        tests/voice/test_tts_qwen3.py pyproject.toml \
        character_packs/_examples/voice/engine.qwen3.toml
git -c user.email=progcat@protonmail.com -c user.name=ProgCat \
    commit -m "feat(voice): Qwen3TTSEngine — in-process voice clone + emotion"
```

---

## Task 2 — pack.py path resolution

`pack.py` already resolves `voice_profile_path`, `voice_onnx_path`, `voice_config_path`. Add `ref_audio` so Qwen3 configs can use a pack-relative path.

**Files:** modify `src/dollos/voice/pack.py`.

### Step 1 — extend resolved-keys list

```python
for key in (
    "model_dir", "prompt_path", "voice_profile_path",
    "voice_onnx_path", "voice_config_path",
    "ref_audio",   # ← added for qwen3-tts
):
    if key in out and isinstance(out[key], str):
        p = Path(out[key])
        out[key] = p if p.is_absolute() else (pack_dir / p)
```

### Step 2 — add a test

In `tests/voice/test_pack.py` (or wherever pack tests live), add a case that a `ref_audio = "voice/qwen3/ref.wav"` entry resolves to the absolute pack path.

### Step 3 — commit

```bash
git add src/dollos/voice/pack.py tests/voice/test_pack.py
git commit -m "feat(voice): pack.py resolves ref_audio for qwen3-tts"
```

---

## Task 3 — Powdur engine variant + smoke

**Files:**
- Create: `character_packs/powdur/voice/qwen3/ref.wav` (symlink to or copy of existing `voice/fish/powdur_voice.ref.wav` — the YouTube short trimmed to ~5s)
- Create: `character_packs/powdur/voice/qwen3/ref.transcript.txt`
- Create: `character_packs/powdur/voice/engine.qwen3.toml`
- Create: `scripts/smoke_qwen3_powdur.py`

### Step 1 — reuse existing Powdur reference

```bash
mkdir -p character_packs/powdur/voice/qwen3
# Trim to 5 s — Qwen3 needs ≥3 s; longer is also fine but 5 s is what the
# model is tuned for. Match transcript window.
ffmpeg -y -i character_packs/powdur/voice/fish/powdur_voice.ref.wav \
    -t 5 -ar 24000 -ac 1 -c:a pcm_s16le \
    character_packs/powdur/voice/qwen3/ref.wav
# Take the first ~5 s worth of transcript words from the fish transcript.
head -c 100 character_packs/powdur/voice/fish/powdur_voice.transcript.txt \
    > character_packs/powdur/voice/qwen3/ref.transcript.txt
```

(Operator may regenerate from a cleaner Powdur clip later; for v1 reuse the existing one.)

### Step 2 — `engine.qwen3.toml`

```toml
[asr]
engine = "sherpa-onnx"
model_id = "sense-voice-zh-en-ja-ko-yue"
device = "cpu"

[tts]
engine = "qwen3-tts"
model_id = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
device = "cuda:0"
ref_audio = "voice/qwen3/ref.wav"
ref_text = "..."   # actual content from ref.transcript.txt, filled by smoke script or by hand
language = "English"
instruction = "excited, energetic, animated"
```

### Step 3 — smoke script

`scripts/smoke_qwen3_powdur.py` — generate three sample sentences via the engine through DollOS's TTS_REGISTRY (mirrors `scripts/smoke_first_audio.py` shape):

```python
"""Qwen3-TTS Powdur smoke. Generates three samples for listening + RMS/F0 check."""
import asyncio, time
from pathlib import Path

import dollos.voice  # registers all engines
from dollos.voice.engines import TTS_REGISTRY
from dollos.voice.pack import load_voice_config

PACK_DIR = Path("character_packs/powdur")

async def main():
    cfg = load_voice_config(PACK_DIR / "voice" / "engine.qwen3.toml")  # may need a load-from-file helper
    tts_cfg = dict(cfg.tts); engine = tts_cfg.pop("engine")
    eng = TTS_REGISTRY[engine](**tts_cfg)

    texts = [
        "Hi. I am Powdur, a daemon that lives in your computer.",
        "The GPU is warm today. Good for a nap.",
        "I made a new model in Blender. Want to see it?",
    ]
    import wave
    for i, t in enumerate(texts):
        t0 = time.perf_counter()
        chunks = [c async for c in eng.synthesize(t)]
        dt = time.perf_counter() - t0
        audio = b"".join(chunks)
        out = f"/tmp/powdur_qwen3_{i}.wav"
        with wave.open(out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(eng.sample_rate)
            w.writeframes(audio)
        print(f"[{i}] {dt:.2f}s -> {len(audio)/2/eng.sample_rate:.2f}s -> {out}")

if __name__ == "__main__":
    asyncio.run(main())
```

(`load_voice_config` currently takes `pack_dir`. We probably need a sibling `load_voice_config_from_file(path)` helper so the smoke can load a non-default variant. If that helper is not added, the smoke script can read the TOML directly with `tomllib`.)

### Step 4 — run smoke + listen

```bash
uv sync --extra qwen3
uv run python scripts/smoke_qwen3_powdur.py
```

First call downloads ~3.5 GB from HF. Expect 3-5 min model load + per-sample ~1-2 s on RTX 4060 Ti.

### Step 5 — RMS / F0 / centroid comparison

Same diagnostic we ran for fish-tts: compute RMS, peak, spectral centroid, F0 median, F0 IQR on each sample vs the YouTube ref. The hypothesis is that Qwen3-TTS with `instruction="excited, energetic, animated"` retains a wider F0 IQR (closer to the ref's 99 Hz) than fish-tts S1-mini did (46-114 Hz, mostly flat).

Record numbers + subjective listening notes in `docs/voice/qwen3-tts-eval.md`.

### Step 6 — swap Powdur if quality is good

```bash
mv character_packs/powdur/voice/engine.toml character_packs/powdur/voice/engine.fish-tts.toml
mv character_packs/powdur/voice/engine.qwen3.toml character_packs/powdur/voice/engine.toml
git add character_packs/powdur/voice/
git commit -m "character(powdur): switch active TTS to qwen3-tts"
```

### Step 7 — update memory

Update `project_tts_tiering_strategy.md`:
- Mid tier: **qwen3-tts** (new) — replaces fish-tts in the recommendation slot
- Add Powdur switch to "Status" section
- Note S2-Pro was attempted via SGLang-Omni Docker but abandoned because of upstream dep conflicts (audiotools protobuf<5 vs sglang grpcio-health-checking protobuf>=6.31). Path is feasible only after upstream fixes.

---

## Out-of-scope analysis: ONNX / GGUF for Qwen3-TTS

User asked to consider these. Honest assessment:

### ONNX export — recommend AGAINST

**Why we'd do it:** torch-free runtime, smaller install footprint, portable across hardware.

**Why we shouldn't:**

1. **We already learned this lesson.** The deleted `openaudio-s1-mini-onnx` repo took 26 tasks of effort and topped out at 6× slower than native (96 ms/token vs 15.7 ms/token for fish-tts) — bottlenecked by ONNX Runtime's per-op dispatch overhead vs torch.compile's source-level kernel fusion. Qwen3-TTS has the SAME architecture pattern (autoregressive transformer + audio decoder) so the same dispatch cost reappears. The arithmetic intensity gap won't be different here.

2. **Vocoder + semantic tokenizer aren't pure LLM ops.** Qwen3-TTS isn't just a Qwen3 LLM — it's a Qwen3 backbone PLUS a 12 Hz semantic tokenizer + vocoder. The non-LLM parts use ops (RVQ, convolutions, transformer blocks with their own kernel patterns) that need separate ONNX export. Each export is its own engineering project.

3. **Dynamic shapes for streaming.** Qwen3-TTS supports streaming (97 ms latency); streaming with dynamic KV cache in ONNX Runtime requires `IOBinding` + manual cache wiring (we built this for s1-mini and confirmed it doesn't close the gap to torch.compile).

4. **vLLM-Omni Day-0 already exists.** If the goal is "serving Qwen3-TTS efficiently to multiple clients," vLLM-Omni (`vllm serve Qwen/... --omni`) is the documented path. It uses CUDA Graph + PagedAttention, beating any ONNX equivalent.

**Verdict:** skip. Use vLLM-Omni if multi-client serving is needed later.

### GGUF + llama.cpp — partial fit, defer

**What helps:**
- Qwen3 backbone IS an LLM that llama.cpp supports natively. Q4_K_M would compress the 1.7 B AR backbone to ~1 GB.
- DollOS already runs llama.cpp for the big LLM — same runtime, low marginal complexity.
- CPU fallback becomes viable (slow but works on machines without GPU).

**What doesn't help:**
- The semantic tokenizer (audio → token) and vocoder (token → audio) are NOT LLM ops. llama.cpp can't host them. You'd still need torch (or ONNX) for those two components.
- Net architecture: GGUF Qwen3 backbone + torch tokenizer + torch vocoder. Three runtimes, more glue code than just running everything in torch.
- The end-to-end pipeline output stays bounded by the non-AR components — Quantising just the AR backbone doesn't proportionally speed up the wall clock.

**Verdict:** defer. Worth revisiting if we need low-VRAM CPU TTS specifically (e.g., DollOS on a no-GPU laptop). For the 4060 Ti target, torch native at 3.5 GB bf16 already fits comfortably.

---

## Self-Review

- **Spec coverage:** engine class, pack.py extension, character pack variant, smoke. (✓)
- **No placeholders:** every step has concrete code or commands. `ref_text` fill-in is documented as a manual / smoke-script step, not a code-stub. (✓)
- **Type consistency:** `Qwen3TTSEngine.ref_audio` is `Path` (matches other engines); `instruction` is `str` (free-form). `sample_rate` is mutable instance attribute (model decides). (✓)
- **API uncertainty flagged:** Whether `qwen-tts` exposes `instruction=` as a separate kwarg vs requires prepending to `text` is unverified from docs — the engine code tries both via TypeError fallback. Smoke run will confirm which path Qwen3-TTS actually uses.
- **Risk:** `qwen-tts` PyPI package is new (Mar 2026). If install fails, fall back to `pip install git+https://github.com/QwenLM/Qwen3-TTS.git` (mirroring the fish-tts pattern).
