"""Tests for the doll pack loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.character import DollPack, Enforcement


_REPO_DOLL_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "doll"
_REPO_HELPER_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "helper"


def test_load_valid_pack():
    pack = DollPack.load(_REPO_DOLL_PACK)
    assert pack.meta.id == "doll"
    assert pack.meta.name == "Doll"
    assert pack.identity.self.strip()
    assert pack.identity.personality.strip()
    assert pack.identity.taboos.strip()


def test_load_missing_doll_toml(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        DollPack.load(tmp_path)


def test_load_missing_meta_section(tmp_path: Path):
    (tmp_path / "doll.toml").write_text(
        '[identity]\nself = "x"\npersonality = "y"\ntaboos = "z"\n'
    )
    with pytest.raises(ValidationError):
        DollPack.load(tmp_path)


def test_load_missing_identity_section(tmp_path: Path):
    (tmp_path / "doll.toml").write_text('[meta]\nid = "x"\nname = "Y"\n')
    with pytest.raises(ValidationError):
        DollPack.load(tmp_path)


def test_load_partial_identity(tmp_path: Path):
    (tmp_path / "doll.toml").write_text(
        '[meta]\n'
        'id = "x"\n'
        'name = "Y"\n'
        '\n'
        '[identity]\n'
        'self = "a"\n'
        'personality = "b"\n'
        # missing taboos
    )
    with pytest.raises(ValidationError):
        DollPack.load(tmp_path)


# --- Enforcement (spec 2026-07-01 persona-hardening §2) ---


def test_enforcement_defaults_when_absent():
    """A pack whose doll.toml has no [enforcement] table loads with empty
    defaults — zero behavior change for packs that don't opt in."""
    pack = DollPack.load(_REPO_HELPER_PACK)
    assert pack.enforcement == Enforcement()
    assert pack.enforcement.banned_substrings == []
    assert pack.enforcement.max_exclaim_run is None


def test_enforcement_explicit_values_parsed(tmp_path: Path):
    (tmp_path / "doll.toml").write_text(
        '[meta]\n'
        'id = "x"\n'
        'name = "Y"\n'
        '\n'
        '[identity]\n'
        'self = "a"\n'
        'personality = "b"\n'
        'taboos = "c"\n'
        '\n'
        '[enforcement]\n'
        'banned_substrings = ["a~", "a〜"]\n'
        'max_exclaim_run = 1\n'
    )
    pack = DollPack.load(tmp_path)
    assert pack.enforcement.banned_substrings == ["a~", "a〜"]
    assert pack.enforcement.max_exclaim_run == 1


def test_enforcement_rejects_unknown_keys(tmp_path: Path):
    (tmp_path / "doll.toml").write_text(
        '[meta]\n'
        'id = "x"\n'
        'name = "Y"\n'
        '\n'
        '[identity]\n'
        'self = "a"\n'
        'personality = "b"\n'
        'taboos = "c"\n'
        '\n'
        '[enforcement]\n'
        'unknown_field = "nope"\n'
    )
    with pytest.raises(ValidationError):
        DollPack.load(tmp_path)


def test_doll_pack_declares_enforcement_rules():
    """Doll's shipped doll.toml opts in with the concrete a~ / exclaim rules
    (spec §2 'Doll's concrete rules')."""
    pack = DollPack.load(_REPO_DOLL_PACK)
    assert "a~" in pack.enforcement.banned_substrings
    assert pack.enforcement.max_exclaim_run == 1


def test_doll_pack_declares_chinese_alias_seed():
    """M6 migration (2026-07-06 self-learned-aliases spec §3.6/D1b, Part A
    A5): the bridge's dead `name_aliases=["Doll", "doll", "娃娃"]` config is
    removed, so the Chinese name「娃娃」must be ported into the pack's own
    `[meta] aliases` seed or cold-start silently loses 娃娃-wake. It's 2 CJK
    chars, clearing A3's `passes_alias_guard` (MIN_ALIAS_LEN=2)."""
    pack = DollPack.load(_REPO_DOLL_PACK)
    assert pack.meta.aliases == ["娃娃"]


# --- Doll speaks English (2026-10-18 doll-english-luxtts plan, Task 6) ---


def test_doll_personality_says_she_talks_and_writes_in_english():
    """Same shape as helper's CN-in / EN-out rule (test_helper_pack.py): the
    language is one plain fact in her personality, not a command. All-English
    plan Task 2: it covers what she writes as well as what she says."""
    pack = DollPack.load(_REPO_DOLL_PACK)
    assert ("- I understand both Chinese and English, but I talk and write "
            "in English.") in pack.identity.personality


def test_doll_pack_carries_chinese_restatement_reference():
    """The Chinese factory self + personality stays in the pack for the keeper
    restatement gate. Its first paragraph ends like the English version (lives
    on the owner's Mac), not the old AI-companion line."""
    pack = DollPack.load(_REPO_DOLL_PACK)
    ref = pack.identity.restatement_reference
    assert ref.strip()
    assert "我就是我本人" in ref
    assert "AI 伴侶" not in ref


def test_restatement_reference_defaults_to_empty():
    """Optional field: a pack without it (helper) loads with an empty string."""
    pack = DollPack.load(_REPO_HELPER_PACK)
    assert pack.identity.restatement_reference == ""
