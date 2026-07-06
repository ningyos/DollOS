"""Tests for the doll pack loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.character import DollPack, Enforcement


_REPO_GURA_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "gura"
_REPO_YESMAN_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "yesman"


def test_load_valid_pack():
    pack = DollPack.load(_REPO_GURA_PACK)
    assert pack.meta.id == "gura"
    assert pack.meta.name == "Gura"
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
    pack = DollPack.load(_REPO_YESMAN_PACK)
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


def test_gura_pack_declares_enforcement_rules():
    """Gura's shipped doll.toml opts in with the concrete a~ / exclaim rules
    (spec §2 'Gura's concrete rules')."""
    pack = DollPack.load(_REPO_GURA_PACK)
    assert "a~" in pack.enforcement.banned_substrings
    assert pack.enforcement.max_exclaim_run == 1


def test_gura_pack_declares_chinese_alias_seed():
    """M6 migration (2026-07-06 self-learned-aliases spec §3.6/D1b, Part A
    A5): the bridge's dead `name_aliases=["Gura", "gura", "古拉"]` config is
    removed, so the Chinese name「古拉」must be ported into the pack's own
    `[meta] aliases` seed or cold-start silently loses 古拉-wake. It's 2 CJK
    chars, clearing A3's `passes_alias_guard` (MIN_ALIAS_LEN=2)."""
    pack = DollPack.load(_REPO_GURA_PACK)
    assert pack.meta.aliases == ["古拉"]
