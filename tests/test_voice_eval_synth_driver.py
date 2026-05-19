import pytest
from pathlib import Path


def test_test_corpus_is_ten_english():
    from dollos.voice_eval.synth_driver import TEST_CORPUS
    assert len(TEST_CORPUS) == 10
    assert all(isinstance(s, str) and s.strip() for s in TEST_CORPUS)


def test_discover_qwen3_engine(tmp_path):
    pack = tmp_path / "fakepack"
    (pack / "voice").mkdir(parents=True)
    (pack / "voice" / "ref.wav").write_bytes(b"RIFFxxxxWAVEfmt ")
    (pack / "voice" / "engine.toml").write_text('''[tts.qwen3-tts]
ref_audio = "voice/ref.wav"
ref_text = "hello there"
language = "English"
''')
    from dollos.voice_eval.synth_driver import discover_engine_kwargs
    name, kwargs, ref_audio, ref_text = discover_engine_kwargs(pack)
    assert name == "qwen3-tts"
    assert ref_text == "hello there"
    assert ref_audio.name == "ref.wav"
    assert ref_audio.is_absolute()


def test_discover_strips_eq_curve_path(tmp_path):
    """eq_curve_path is stripped — eval measures raw engine output."""
    pack = tmp_path / "fakepack"
    (pack / "voice").mkdir(parents=True)
    (pack / "voice" / "ref.wav").write_bytes(b"RIFFxxxxWAVEfmt ")
    (pack / "voice" / "eq.json").write_text("{}")
    (pack / "voice" / "engine.toml").write_text('''[tts.qwen3-tts]
ref_audio = "voice/ref.wav"
ref_text = "hi"
eq_curve_path = "voice/eq.json"
''')
    from dollos.voice_eval.synth_driver import discover_engine_kwargs
    _name, kwargs, _ref, _txt = discover_engine_kwargs(pack)
    assert "eq_curve_path" not in kwargs


def test_discover_piper_raises(tmp_path):
    pack = tmp_path / "fakepack"
    (pack / "voice").mkdir(parents=True)
    (pack / "voice" / "voice.onnx").write_bytes(b"")
    (pack / "voice" / "engine.toml").write_text('''[tts.piper]
voice_onnx_path = "voice/voice.onnx"
voice_config_path = "voice/voice.onnx.json"
''')
    (pack / "voice" / "voice.onnx.json").write_text("{}")
    from dollos.voice_eval.synth_driver import discover_engine_kwargs
    with pytest.raises(ValueError, match="no reference audio"):
        discover_engine_kwargs(pack)
