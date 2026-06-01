from pathlib import Path

from dollos.character import DollPack

PACK = Path("character_packs/yesman")


def test_yesman_pack_loads():
    pack = DollPack.load(PACK)
    assert pack.meta.id == "yesman"
    assert pack.meta.name == "Yes Man"


def test_yesman_identity_fields_present():
    pack = DollPack.load(PACK)
    assert pack.identity.self.strip()
    assert pack.identity.personality.strip()
    assert pack.identity.taboos.strip()


def test_yesman_replies_in_english_rule():
    # Personality must encode the CN-in / EN-out rule (spec §3.2).
    pack = DollPack.load(PACK)
    assert "English" in pack.identity.personality


def test_yesman_no_larp_taboo():
    pack = DollPack.load(PACK)
    assert "LARP" in pack.identity.taboos
