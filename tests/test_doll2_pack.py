from pathlib import Path

from dollos.character import BASE_BANNED, DollPack

PACK = Path(__file__).resolve().parent.parent / "character_packs" / "doll2"


def test_doll2_pack_loads():
    pack = DollPack.load(PACK)
    assert pack.meta.id == "doll2"
    assert pack.meta.name == "娃娃"
    assert pack.meta.aliases == ["娃娃"]
    assert pack.meta.language == "zh-TW"


def test_doll2_enforcement():
    pack = DollPack.load(PACK)
    assert pack.enforcement.max_exclaim_run == 1
    assert pack.enforcement.banned_substrings == BASE_BANNED["zh-TW"]


def test_doll2_thinks_she_is_deepseek():
    pack = DollPack.load(PACK)
    assert "DeepSeek" in pack.identity.self
