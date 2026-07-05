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


# ---------------------------------------------------------------------------
# Whole-branch review I3: external_public/ + external_dm/ must be indexed
# too, or a full reindex silently drops external-tier memory written by
# NoteMemory/WriteDiary (which index individual files immediately, but a
# full memsearch.index() walk previously covered only
# [shared, transcripts, skills]).
# ---------------------------------------------------------------------------


def test_build_memsearch_creates_external_public_dir(tmp_path: Path):
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "external_public"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


def test_build_memsearch_creates_external_dm_dir(tmp_path: Path):
    settings = _make_settings(tmp_path)
    expected = tmp_path / "data" / "memory" / "external_dm"
    assert not expected.exists()

    build_memsearch(settings)

    assert expected.is_dir()


async def test_build_memsearch_full_reindex_retains_external_dm_note(tmp_path: Path):
    """The actual I3 teeth test: a NOTE written straight to external_dm/ (as
    NoteMemory would write on an owner-DM turn) must still be retrievable
    after a FULL reindex — not just after the per-file index_file() call
    NoteMemory itself does. Before the fix, build_memsearch's paths list
    ([shared, transcripts, skills]) did not include external_dm/, so a full
    index() would silently drop this note from the FTS index."""
    settings = _make_settings(tmp_path)
    mem = build_memsearch(settings)
    try:
        dm_dir = tmp_path / "data" / "memory" / "external_dm"
        dm_dir.mkdir(parents=True, exist_ok=True)
        (dm_dir / "2026-07-05.md").write_text(
            "## h\n\nowner-dm-note-unique-marker-i3-check.\n", encoding="utf-8"
        )
        await mem.index()  # FULL reindex, not index_file()
        hits = await mem.search("owner-dm-note-unique-marker-i3-check", top_k=5)
        assert any(
            "owner-dm-note-unique-marker-i3-check" in h["content"] for h in hits
        )
    finally:
        mem.close()


async def test_build_memsearch_full_reindex_retains_external_public_note(tmp_path: Path):
    """Same as above for external_public/ (NoteMemory on a stranger's turn)."""
    settings = _make_settings(tmp_path)
    mem = build_memsearch(settings)
    try:
        pub_dir = tmp_path / "data" / "memory" / "external_public"
        pub_dir.mkdir(parents=True, exist_ok=True)
        (pub_dir / "2026-07-05.md").write_text(
            "## h\n\npublic-note-unique-marker-i3-check.\n", encoding="utf-8"
        )
        await mem.index()  # FULL reindex
        hits = await mem.search("public-note-unique-marker-i3-check", top_k=5)
        assert any(
            "public-note-unique-marker-i3-check" in h["content"] for h in hits
        )
    finally:
        mem.close()
