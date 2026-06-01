from pathlib import Path

from dollos.character import DollPack

PACK = Path("character_packs/helper")


def test_helper_pack_loads():
    pack = DollPack.load(PACK)
    assert pack.meta.id == "helper"
    assert pack.meta.name == "Helper"


def test_helper_identity_fields_present():
    pack = DollPack.load(PACK)
    assert pack.identity.self.strip()
    assert pack.identity.personality.strip()
    assert pack.identity.taboos.strip()


def test_helper_replies_in_english_rule():
    # Personality must encode the CN-in / EN-out rule (spec §3.2).
    pack = DollPack.load(PACK)
    assert "English" in pack.identity.personality


def test_helper_no_larp_taboo():
    pack = DollPack.load(PACK)
    assert "LARP" in pack.identity.taboos
