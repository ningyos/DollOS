"""``build_alias_provider`` — kernel-side closure that feeds L0 name-wake
(2026-07-06 self-learned-aliases spec §3.5, A3 — the task that closes the
loop from owner-taught ``LearnName`` writes back to ``AttentionGate``).

Covers: union of the 3 sources (pack seed / config floor / learned tokens),
the load-time mechanical guard on seeds+floor (I3 — a denylisted/too-short
pack name or config entry is dropped + logged, never silently trusted),
the mtime-gated cache (no re-read of name_aliases.json when it hasn't
changed), and fail-closed behavior when ``stat()`` fails.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from dollos.character import DollPack
from dollos.config import (
    AttentionSettings,
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import build_alias_provider
from dollos.mind.attention import AttentionGate
from dollos.mind.name_aliases import NameAliasStore


def _make_settings(
    tmp_path: Path,
    *,
    pack_name: str = "Doll",
    pack_aliases: list[str] | None = None,
    config_name_aliases: list[str] | None = None,
) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    aliases_line = ""
    if pack_aliases:
        quoted = ", ".join(f'"{a}"' for a in pack_aliases)
        aliases_line = f"aliases = [{quoted}]\n"
    (pack_dir / "doll.toml").write_text(
        "[meta]\n"
        'id = "doll"\n'
        f'name = "{pack_name}"\n'
        f"{aliases_line}"
        "\n"
        "[identity]\n"
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
        attention=AttentionSettings(
            name_aliases=config_name_aliases or [],
        ),
    )


def _load_pack(settings: Settings) -> DollPack:
    return DollPack.load(settings.character.pack)


def _bump_mtime(path: Path) -> None:
    """Force ``path``'s mtime strictly forward regardless of filesystem
    mtime resolution, so mtime-cache invalidation tests aren't flaky when
    two writes happen to land in the same clock tick (a real owner-taught
    write and a later remove are always far apart in wall-clock terms;
    only back-to-back test writes risk colliding)."""
    current = path.stat().st_mtime
    os.utime(path, (current + 1.0, current + 1.0))


# ----- union of the 3 sources -----


def test_provider_unions_seed_config_and_learned_tokens(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        pack_name="Doll",
        pack_aliases=["小鯊"],
        config_name_aliases=["adminfloor"],
    )
    pack = _load_pack(settings)

    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")
    store.add("shork", now=time.time())

    provider = build_alias_provider(settings, pack)
    tokens = provider()

    assert tokens == frozenset({"Doll", "小鯊", "adminfloor", "shork"})


def test_provider_reflects_learned_tokens_removed(tmp_path: Path) -> None:
    """Learned tokens are re-read (mtime changed) on removal too — an
    owner un-teaching a nickname must stop it from waking her."""
    settings = _make_settings(tmp_path, pack_name="Doll")
    pack = _load_pack(settings)
    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")
    store.add("shork", now=time.time())

    provider = build_alias_provider(settings, pack)
    assert "shork" in provider()

    store.remove("shork")
    _bump_mtime(memory_root / "name_aliases.json")
    assert "shork" not in provider()


# ----- load-time seed guard (I3) -----


def test_denylisted_short_pack_name_is_dropped_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec example: a pack whose meta.name is '你' — both too-short (<2)
    and denylisted. It must be DROPPED from the wake-eligible set (that
    pack's name-wake silently won't fire) and a warning logged — never
    silently trusted just because it's an 'unprunable' seed."""
    settings = _make_settings(tmp_path, pack_name="你")
    pack = _load_pack(settings)

    with caplog.at_level(logging.WARNING):
        provider = build_alias_provider(settings, pack)
        tokens = provider()

    assert "你" not in tokens
    assert any("你" in rec.message for rec in caplog.records)


def test_valid_pack_alias_survives_alongside_a_dropped_bad_one(tmp_path: Path) -> None:
    """Only the bad token is dropped — a co-declared valid alias still
    makes it into the wake-eligible set."""
    settings = _make_settings(tmp_path, pack_name="你", pack_aliases=["小鯊"])
    pack = _load_pack(settings)

    provider = build_alias_provider(settings, pack)
    tokens = provider()

    assert "你" not in tokens
    assert "小鯊" in tokens


def test_denylisted_config_floor_entry_is_dropped(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path, pack_name="Doll", config_name_aliases=["hey", "adminfloor"]
    )
    pack = _load_pack(settings)

    provider = build_alias_provider(settings, pack)
    tokens = provider()

    assert "hey" not in tokens
    assert "adminfloor" in tokens


# ----- mtime-gated cache -----


def test_mtime_cache_skips_reread_when_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path, pack_name="Doll")
    pack = _load_pack(settings)
    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")
    store.add("shork", now=time.time())  # creates the file with a real mtime

    calls: list[int] = []
    original = NameAliasStore.active_tokens

    def _spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(NameAliasStore, "active_tokens", _spy)

    provider = build_alias_provider(settings, pack)
    provider()
    provider()
    provider()

    assert len(calls) == 1  # only the first call actually re-read the file


def test_mtime_cache_rereads_after_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path, pack_name="Doll")
    pack = _load_pack(settings)
    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")
    store.add("shork", now=time.time())

    calls: list[int] = []
    original = NameAliasStore.active_tokens

    def _spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(NameAliasStore, "active_tokens", _spy)

    provider = build_alias_provider(settings, pack)
    provider()
    assert len(calls) == 1

    store.add("second", now=time.time())
    _bump_mtime(memory_root / "name_aliases.json")  # guarantee mtime changed

    tokens = provider()
    assert len(calls) == 2
    assert "second" in tokens


# ----- fail-closed on stat() failure -----


def test_missing_alias_file_fails_closed_to_seed_and_floor(tmp_path: Path) -> None:
    """No name_aliases.json has ever been written (cold start) — stat()
    raises FileNotFoundError. Must not raise; must return exactly the
    seed+floor set."""
    settings = _make_settings(
        tmp_path,
        pack_name="Doll",
        pack_aliases=["小鯊"],
        config_name_aliases=["adminfloor"],
    )
    pack = _load_pack(settings)

    provider = build_alias_provider(settings, pack)
    tokens = provider()  # must not raise

    assert tokens == frozenset({"Doll", "小鯊", "adminfloor"})


def test_missing_alias_file_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Part A whole-branch review, Minor fix 1: a fresh install / early
    dogfood where the owner hasn't taught a nickname yet is the DEFAULT,
    long-lived state — name_aliases.json simply doesn't exist. This must
    NOT emit a WARNING (that would be log spam on the L0 hot path for
    every qualifying public message for the life of the install). It
    still fails closed to the seed+floor set."""
    settings = _make_settings(
        tmp_path,
        pack_name="Doll",
        pack_aliases=["小鯊"],
        config_name_aliases=["adminfloor"],
    )
    pack = _load_pack(settings)

    with caplog.at_level(logging.DEBUG):
        provider = build_alias_provider(settings, pack)
        tokens = provider()

    assert tokens == frozenset({"Doll", "小鯊", "adminfloor"})
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_stat_failure_after_successful_read_returns_last_good_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Defense in depth beyond the cold-start case: if the file was read
    successfully once (so learned tokens are in the cache) and a LATER
    call's stat() raises a GENUINE (non-FileNotFound) OSError
    (permissions/transient), the provider must return the last-good
    frozenset (including the learned token), not regress to just
    seed+floor, must not raise, and — unlike the missing-file case above —
    must still emit a WARNING (this is a real anomaly, not the expected
    cold-start state)."""
    settings = _make_settings(tmp_path, pack_name="Doll")
    pack = _load_pack(settings)
    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")
    store.add("shork", now=time.time())

    provider = build_alias_provider(settings, pack)
    first = provider()
    assert "shork" in first

    real_stat = Path.stat

    def _raising_stat(self, *a, **kw):
        if self.name == "name_aliases.json":
            raise OSError("simulated transient stat failure")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _raising_stat)

    with caplog.at_level(logging.WARNING):
        second = provider()  # must not raise
    assert second == first
    assert "shork" in second
    assert any(
        rec.levelno == logging.WARNING and "stat failed" in rec.message
        for rec in caplog.records
    )


# ----- end-to-end: the provider actually feeds a real AttentionGate -----


def test_provider_wired_into_attention_gate_admits_learned_alias(
    tmp_path: Path,
) -> None:
    """Proves the loop A3 closes: a token written via NameAliasStore (what
    LearnName does) becomes wake-eligible through build_alias_provider
    without any daemon restart."""
    settings = _make_settings(tmp_path, pack_name="Doll")
    pack = _load_pack(settings)
    memory_root = settings.data.root / "memory"
    store = NameAliasStore(memory_root / "name_aliases.json")

    provider = build_alias_provider(settings, pack)
    gate = AttentionGate(
        alias_provider=provider,
        always_wake_channels=(),
        owner_id="owner",
        max_session_turns=6,
        window_base_s=90.0,
        window_decay=0.6,
        debounce_engaged_s=2.0,
        debounce_cold_s=8.0,
    )

    event = {
        "channel_id": "c1", "author_id": "u1", "is_dm": False,
        "mentioned": False, "content": "hey shork are you there",
    }
    before = gate.admit(dict(event), now=100.0)
    assert not before.admit

    store.add("shork", now=time.time())
    after = gate.admit(dict(event), now=101.0)
    assert after.admit and after.reason == "l0_name"
