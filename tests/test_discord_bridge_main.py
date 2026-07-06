"""`_load_bridge_config` (discord_bridge/__main__.py) — Part A A5 + Part B B3.

`name_aliases` / `always_wake_channels` are dead bridge config (P1c moved
L0/L1 wake admission daemon-side into `AttentionGate`; A3 confirmed the
`alias_provider` is built kernel-side from pack seeds + daemon config floor
+ learned tokens — the bridge never participates). A5 removes both fields
from `BridgeConfig` and stops `_load_bridge_config` from reading them.

`channel_allowlist` is ALSO dead bridge config (2026-07-06 spec §4.3, Part
B / B3): it never gated forwarding (P1c already made the bridge
forward-all; B2's `owner_guild_only` is the real gate) — its only two uses
(seeding reply-routing pre-register + the reconnect-backfill channel list)
are superseded by register-on-first-forward and the new, OPTIONAL
`backfill_channels` field respectively. B3 removes `channel_allowlist` from
`BridgeConfig` and stops `_load_bridge_config` from reading it, the same
way A5 did for the other two dead fields.

Per the plan's constraint: `_load_bridge_config` must NOT reject a
`bridge.toml` that still has a leftover `name_aliases`/`always_wake_channels`/
`channel_allowlist` key (users may have an old file lying around) — it just
stops reading them. `tomllib.load` + `dict.get`/indexing on the fields we DO
want means extra keys are silently ignored; no strict-key validation is
added (spec: no fallback logic, but this isn't a fallback — it's just not
reading a key that no longer maps to any field).
"""
from __future__ import annotations

from pathlib import Path

from dollos.discord_bridge.__main__ import _load_bridge_config
from dollos.discord_bridge.controller import BridgeConfig


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bridge.toml"
    path.write_text(body)
    return path


def test_load_bridge_config_without_dead_keys(tmp_path: Path):
    """A bridge.toml with none of the dead keys loads fine — the common
    case post-B3 (bridge.example.toml no longer has any of them)."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-1"
owner_discord_id = "111"
""",
    )
    token, cfg = _load_bridge_config(path)
    assert token == "tok-1"
    assert isinstance(cfg, BridgeConfig)
    assert cfg.owner_id == "111"
    assert cfg.backfill_channels == []
    assert not hasattr(cfg, "channel_allowlist")
    assert not hasattr(cfg, "name_aliases")
    assert not hasattr(cfg, "always_wake_channels")


def test_load_bridge_config_ignores_leftover_dead_keys(tmp_path: Path):
    """A bridge.toml carrying OLD `channel_allowlist` / `name_aliases` /
    `always_wake_channels` keys (a pre-B3 file a user hasn't regenerated)
    must not crash the loader — the keys are just never read, not
    validated against."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-2"
owner_discord_id = "222"
channel_allowlist = ["c1"]
name_aliases = ["Gura", "gura", "古拉"]
always_wake_channels = ["vip"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert token == "tok-2"
    assert cfg.owner_id == "222"
    assert not hasattr(cfg, "channel_allowlist")
    assert cfg.backfill_channels == []


def test_load_bridge_config_reads_backfill_channels(tmp_path: Path):
    """`backfill_channels` (2026-07-06 spec §4.3, Part B / B3, D5(ii)) is
    OPTIONAL and, when present, is read verbatim onto `BridgeConfig` —
    decoupled from `owner_guild_only`'s wake scope."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-3"
owner_discord_id = "333"
backfill_channels = ["c1", "c2"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.backfill_channels == ["c1", "c2"]


def test_load_bridge_config_defaults_backfill_channels_to_empty(tmp_path: Path):
    """No `backfill_channels` key at all -> `[]`, i.e. no reconnect-gap
    backfill — the safe/predictable default (D5(ii))."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-4"
owner_discord_id = "444"
""",
    )
    token, cfg = _load_bridge_config(path)
    assert cfg.backfill_channels == []
