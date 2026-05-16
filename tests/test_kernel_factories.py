"""Tests for kernel factory functions."""

from pathlib import Path

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import build_memsearch


def _make_settings(tmp_path: Path) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
        'self = "You are Doll."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )
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
        character=CharacterConfig(pack=pack_dir),
    )


def test_build_memsearch_creates_shared_dir(tmp_path: Path):
    """build_memsearch should create data.root/memory/shared/ if missing."""
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "shared"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


def test_build_memsearch_creates_transcripts_dir(tmp_path: Path):
    """build_memsearch should create data.root/memory/transcripts/ if missing."""
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "transcripts"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


def test_build_memsearch_returns_memsearch_instance(tmp_path: Path):
    settings = _make_settings(tmp_path)
    instance = build_memsearch(settings)
    assert hasattr(instance, "search")
    assert callable(instance.search)


def test_build_memsearch_indexes_skills_dir(tmp_path):
    settings = _make_settings(tmp_path)
    build_memsearch(settings)
    skills_path = tmp_path / "data" / "memory" / "skills"
    assert skills_path.is_dir()


def test_build_memsearch_does_not_create_skill_bodies_dir(tmp_path):
    """skill_bodies/ is not indexed and should not be auto-created at startup."""
    settings = _make_settings(tmp_path)
    build_memsearch(settings)
    bodies_path = tmp_path / "data" / "memory" / "skill_bodies"
    assert not bodies_path.exists()
