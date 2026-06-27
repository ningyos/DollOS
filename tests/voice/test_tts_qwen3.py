"""Qwen3TTSEngine tests with mocked qwen_tts package."""
from __future__ import annotations
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock
import numpy as np
import pytest


@dataclass
class _FakeVoiceClonePromptItem:
    ref_code: Any = None
    ref_spk_embedding: Any = None
    x_vector_only_mode: bool = False
    icl_mode: bool = True
    ref_text: Optional[str] = None


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

    # Provide qwen_tts.inference.qwen3_tts_model.VoiceClonePromptItem so the
    # engine can reconstruct items from .pt payloads in prompt-path mode.
    fake_inference = types.ModuleType("qwen_tts.inference")
    fake_inner = types.ModuleType("qwen_tts.inference.qwen3_tts_model")
    fake_inner.VoiceClonePromptItem = _FakeVoiceClonePromptItem
    fake_module.inference = fake_inference
    fake_inference.qwen3_tts_model = fake_inner

    monkeypatch.setitem(sys.modules, "qwen_tts", fake_module)
    monkeypatch.setitem(sys.modules, "qwen_tts.inference", fake_inference)
    monkeypatch.setitem(sys.modules, "qwen_tts.inference.qwen3_tts_model", fake_inner)

    # Reset module-level singleton so each test gets a fresh model load.
    import dollos.voice.tts_qwen3 as _mod
    monkeypatch.setattr(_mod, "_MODEL", None)
    monkeypatch.setattr(_mod, "_MODEL_ID", None)

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


async def test_qwen3_voice_clone_prompt_path(fake_qwen_tts, tmp_path):
    """Engine loads voice_clone_prompt_path .pt and uses items in synth call."""
    import torch
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    item = {
        "ref_code": torch.zeros((10, 1), dtype=torch.long),
        "ref_spk_embedding": torch.zeros(192, dtype=torch.float32),
        "x_vector_only_mode": False,
        "icl_mode": True,
        "ref_text": "hi",
    }
    pt = tmp_path / "voice_clone_prompt.pt"
    torch.save({"items": [item]}, pt)

    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        voice_clone_prompt_path=pt,
        language="English",
    )
    assert eng._voice_clone_prompt is not None
    assert len(eng._voice_clone_prompt) == 1

    async for _ in eng.synthesize("hello"):
        pass
    _, fake_model = fake_qwen_tts
    call = fake_model.generate_voice_clone.call_args
    assert "voice_clone_prompt" in call.kwargs
    assert "ref_audio" not in call.kwargs
    assert "ref_text" not in call.kwargs


def test_qwen3_rejects_both_modes(fake_qwen_tts, tmp_path):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    ref = tmp_path / "ref.wav"; ref.write_bytes(b"X")
    pt = tmp_path / "voice_clone_prompt.pt"
    import torch
    torch.save({"items": [{
        "ref_code": None,
        "ref_spk_embedding": torch.zeros(8),
        "x_vector_only_mode": True,
        "icl_mode": False,
    }]}, pt)
    with pytest.raises(ValueError, match="exactly one"):
        Qwen3TTSEngine(
            model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            device="cuda:0",
            ref_audio=ref, ref_text="hi",
            voice_clone_prompt_path=pt,
        )


def test_qwen3_rejects_neither_mode(fake_qwen_tts):
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine
    with pytest.raises(ValueError, match="must provide"):
        Qwen3TTSEngine(
            model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            device="cuda:0",
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


# ---------------------------------------------------------------------------
# I8 regression: voice_clone_prompt_path + instruction must honour instruction
# ---------------------------------------------------------------------------

async def test_qwen3_voice_clone_prompt_honours_instruction(fake_qwen_tts, tmp_path):
    """I8: voice_clone_prompt path must pass prefixed_text (with instruction),
    not raw text."""
    import torch
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    item = {
        "ref_code": torch.zeros((10, 1), dtype=torch.long),
        "ref_spk_embedding": torch.zeros(192, dtype=torch.float32),
        "x_vector_only_mode": False,
        "icl_mode": True,
        "ref_text": "hi",
    }
    pt = tmp_path / "voice_clone_prompt.pt"
    torch.save({"items": [item]}, pt)

    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        voice_clone_prompt_path=pt,
        language="English",
        instruction="excited and energetic",
    )
    async for _ in eng.synthesize("hello"):
        pass
    _, fake_model = fake_qwen_tts
    call = fake_model.generate_voice_clone.call_args
    text_arg = call.kwargs.get("text") or (call.args[0] if call.args else "")
    assert "excited and energetic" in text_arg, (
        f"I8: instruction not in text for voice_clone_prompt path; text={text_arg!r}"
    )


# ---------------------------------------------------------------------------
# I9 regression: signature probe instead of try/except TypeError
# ---------------------------------------------------------------------------

async def test_qwen3_uses_instruction_kwarg_when_signature_has_it(fake_qwen_tts, tmp_path):
    """I9: when model signature includes 'instruction', it is passed as kwarg,
    not only prefixed into text."""
    import inspect
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    _, fake_model = fake_qwen_tts
    # Give generate_voice_clone a real signature that includes 'instruction'.
    fake_model.generate_voice_clone.__signature__ = inspect.Signature([
        inspect.Parameter("text", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("language", inspect.Parameter.KEYWORD_ONLY, default="English"),
        inspect.Parameter("ref_audio", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("ref_text", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("instruction", inspect.Parameter.KEYWORD_ONLY, default=None),
    ])

    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="softly",
    )
    assert eng._has_instruction_kwarg is True
    async for _ in eng.synthesize("hello"):
        pass
    call = fake_model.generate_voice_clone.call_args
    # Instruction should be passed as an explicit kwarg.
    assert call.kwargs.get("instruction") == "softly", (
        f"I9: instruction kwarg not passed when signature has it; kwargs={call.kwargs}"
    )


async def test_qwen3_prefixes_text_when_signature_lacks_instruction(fake_qwen_tts, tmp_path):
    """I9: when model signature lacks 'instruction', text is prefixed instead,
    and no TypeError is ever caught."""
    import inspect
    from dollos.voice.tts_qwen3 import Qwen3TTSEngine

    _, fake_model = fake_qwen_tts
    # Give generate_voice_clone a signature WITHOUT 'instruction'.
    fake_model.generate_voice_clone.__signature__ = inspect.Signature([
        inspect.Parameter("text", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("language", inspect.Parameter.KEYWORD_ONLY, default="English"),
        inspect.Parameter("ref_audio", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("ref_text", inspect.Parameter.KEYWORD_ONLY, default=None),
    ])

    ref = tmp_path / "ref.wav"; ref.write_bytes(b"WAVE_FAKE")
    eng = Qwen3TTSEngine(
        model_id="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda:0",
        ref_audio=ref,
        ref_text="hi",
        language="English",
        instruction="softly",
    )
    assert eng._has_instruction_kwarg is False
    async for _ in eng.synthesize("hello"):
        pass
    call = fake_model.generate_voice_clone.call_args
    text_arg = call.kwargs.get("text") or (call.args[0] if call.args else "")
    assert "softly" in text_arg, (
        f"I9: instruction not prefixed into text when signature lacks it; text={text_arg!r}"
    )
    # No 'instruction' kwarg should be passed.
    assert "instruction" not in call.kwargs
