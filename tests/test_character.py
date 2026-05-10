"""Tests for the doll pack loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.character import DollPack


_REPO_GURA_PACK = Path(__file__).resolve().parent.parent / "character_packs" / "gura"


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
