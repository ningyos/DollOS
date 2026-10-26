from pathlib import Path

from dollos.character import BASE_BANNED, DollPack

PACK = Path(__file__).resolve().parent.parent / "character_packs" / "dafeiyu"


def test_dafeiyu_pack_loads():
    pack = DollPack.load(PACK)
    assert pack.meta.id == "dafeiyu"
    assert pack.meta.name == "大肥魚"
    assert pack.meta.aliases == ["肥魚"]
    assert pack.meta.language == "zh-TW"


def test_dafeiyu_enforcement():
    pack = DollPack.load(PACK)
    assert pack.enforcement.max_exclaim_run == 1
    assert pack.enforcement.banned_substrings == BASE_BANNED["zh-TW"]


def test_dafeiyu_thinks_she_is_deepseek():
    pack = DollPack.load(PACK)
    assert "DeepSeek" in pack.identity.self
