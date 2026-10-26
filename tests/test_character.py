"""Tests for the doll pack loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.character import BASE_BANNED, DollPack


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
    """A pack whose doll.toml has no [enforcement] table loads with defaults:
    no exclaim cap, and only the base banned list (base-persona spec §4)."""
    pack = DollPack.load(_REPO_YESMAN_PACK)
    assert pack.enforcement.banned_substrings == BASE_BANNED["en"]
    assert pack.enforcement.base_exempt == []
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
    assert pack.enforcement.banned_substrings == [*BASE_BANNED["en"], "a~", "a〜"]
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


# --- Gura speaks English (2026-10-18 gura-english-luxtts plan, Task 6) ---


def test_gura_personality_says_she_talks_and_writes_in_english():
    """Same shape as yesman's CN-in / EN-out rule (test_yesman_pack.py): the
    language is one plain fact in her personality, not a command. All-English
    plan Task 2: it covers what she writes as well as what she says."""
    pack = DollPack.load(_REPO_GURA_PACK)
    assert ("- I understand both Chinese and English, but I talk and write "
            "in English.") in pack.identity.personality


# --- Base persona: language + base banned substrings (2026-10-25 spec §3.1/§4) ---


def _write_pack(tmp_path: Path, meta_extra: str = "", enforcement: str | None = None) -> Path:
    text = (
        '[meta]\n'
        'id = "x"\n'
        'name = "Y"\n'
        f'{meta_extra}'
        '\n'
        '[identity]\n'
        'self = "a"\n'
        'personality = "b"\n'
        'taboos = "c"\n'
    )
    if enforcement is not None:
        text += '\n[enforcement]\n' + enforcement
    (tmp_path / "doll.toml").write_text(text)
    return tmp_path


def test_language_defaults_to_en(tmp_path: Path):
    pack = DollPack.load(_write_pack(tmp_path))
    assert pack.meta.language == "en"


def test_language_zh_tw_parsed(tmp_path: Path):
    pack = DollPack.load(_write_pack(tmp_path, meta_extra='language = "zh-TW"\n'))
    assert pack.meta.language == "zh-TW"
    assert pack.enforcement.banned_substrings == BASE_BANNED["zh-TW"]


def test_invalid_language_fails_to_load(tmp_path: Path):
    with pytest.raises(ValidationError):
        DollPack.load(_write_pack(tmp_path, meta_extra='language = "ja"\n'))


def test_pack_without_enforcement_gets_base_list(tmp_path: Path):
    pack = DollPack.load(_write_pack(tmp_path))
    assert pack.enforcement.banned_substrings == BASE_BANNED["en"]
    assert pack.enforcement.max_exclaim_run is None


def test_banned_merge_order_and_dedup(tmp_path: Path):
    """Base entries first in their order, then the pack's own; a pack entry
    that repeats a base one (or itself) appears once, at its first spot."""
    pack = DollPack.load(_write_pack(
        tmp_path,
        enforcement='banned_substrings = ["a~", "As an AI", "a~", "uwu"]\n',
    ))
    assert pack.enforcement.banned_substrings == [*BASE_BANNED["en"], "a~", "uwu"]


def test_base_exempt_removes_entry(tmp_path: Path):
    pack = DollPack.load(_write_pack(
        tmp_path,
        enforcement='banned_substrings = ["a~"]\nbase_exempt = ["As an AI"]\n',
    ))
    assert pack.enforcement.banned_substrings == [
        "Great question", "How can I help", "I hope this helps", "a~",
    ]


def test_base_exempt_not_in_base_list_fails(tmp_path: Path):
    """A typo'd exemption fails loudly instead of silently exempting nothing.
    A zh-TW phrase is not exemptable from an en pack either."""
    with pytest.raises(ValueError, match="好問題"):
        DollPack.load(_write_pack(
            tmp_path, enforcement='base_exempt = ["好問題"]\n',
        ))
