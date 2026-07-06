from pathlib import Path

from dollos.mind.name_aliases import NameAliasStore


def _p(tmp_path) -> Path:
    return tmp_path / "name_aliases.json"


def test_add_then_active_tokens_contains_it(tmp_path):
    store = NameAliasStore(_p(tmp_path))
    store.add("阿吉", now=1000.0)
    assert "阿吉" in store.active_tokens()


def test_remove_then_active_tokens_does_not_contain_it(tmp_path):
    store = NameAliasStore(_p(tmp_path))
    store.add("阿吉", now=1000.0)
    store.remove("阿吉")
    assert "阿吉" not in store.active_tokens()


def test_duplicate_add_is_idempotent_no_dup(tmp_path):
    store = NameAliasStore(_p(tmp_path))
    store.add("阿吉", now=1000.0)
    store.add("阿吉", now=2000.0, note="second add")
    tokens = store.active_tokens()
    assert tokens == frozenset({"阿吉"})


def test_corrupt_file_yields_empty_set_no_raise(tmp_path):
    p = _p(tmp_path)
    p.write_text("{ not valid json", encoding="utf-8")
    store = NameAliasStore(p)
    assert store.active_tokens() == frozenset()


def test_missing_file_yields_empty_set_no_raise(tmp_path):
    store = NameAliasStore(_p(tmp_path))
    assert store.active_tokens() == frozenset()


def test_round_trip_new_store_same_path(tmp_path):
    p = _p(tmp_path)
    store1 = NameAliasStore(p)
    store1.add("小吉", now=1000.0)

    store2 = NameAliasStore(p)
    assert "小吉" in store2.active_tokens()


def test_add_sets_state_active_and_origin_owner(tmp_path):
    p = _p(tmp_path)
    store = NameAliasStore(p)
    store.add("阿吉", now=1234.0, note="learned from owner")
    entries = store._load()
    entry = entries["阿吉"]
    assert entry.state == "active"
    assert entry.origin == "owner"
    assert entry.added_at == 1234.0
    assert entry.note == "learned from owner"
