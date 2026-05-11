"""Smoke test the voice prepare CLI flow with luxtts mocked."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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
    ref_wav.write_bytes(b"\x00" * 100)

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
    meta = (pack_dir / "voice" / "luxtts" / "ref.meta.toml").read_text()
    assert "hello" in meta
    assert "ref.wav" in meta
