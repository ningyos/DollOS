"""Tests for kernel factory functions."""

from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.inner_voice import InnerVoice
from dollos.kernel import build_inner_voice, build_memsearch
from dollos.prompts import PromptRenderer


def _make_settings(tmp_path: Path) -> Settings:
    character_path = tmp_path / "character.jinja"
    character_path.write_text("You are Doll.")
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(profile_path=character_path),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=15.0,
        ),
    )


def test_build_memsearch_creates_shared_dir(tmp_path: Path):
    """build_memsearch should create data.root/memory/shared/ if missing."""
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "shared"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


def test_build_memsearch_returns_memsearch_instance(tmp_path: Path):
    settings = _make_settings(tmp_path)
    instance = build_memsearch(settings)
    assert hasattr(instance, "search")
    assert callable(instance.search)


def test_build_inner_voice_returns_innervoice_with_top_k_from_settings(tmp_path: Path):
    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert isinstance(iv, InnerVoice)
    assert iv._default_top_k == 7  # from MemsearchConfig.top_k


def test_build_inner_voice_uses_inner_voice_config_base_url(tmp_path: Path):
    """The factory must point InnerVoice's small-LLM provider at inner_voice.base_url,
    not the main LLM's base_url."""
    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert iv._llm._provider._base_url == "http://test.local:8003"
    assert iv._llm._provider._timeout_s == 15.0


def test_build_inner_voice_uses_qwen3_plain_template(tmp_path: Path):
    from dollos.llm.templates import Qwen3PlainTemplate

    settings = _make_settings(tmp_path)
    memsearch = build_memsearch(settings)
    iv = build_inner_voice(settings, memsearch, PromptRenderer())
    assert isinstance(iv._llm._template, Qwen3PlainTemplate)
