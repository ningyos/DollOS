"""`_load_bridge_config` (discord_bridge/__main__.py) — Part A A5, spec §3.6.

`name_aliases` / `always_wake_channels` are dead bridge config (P1c moved
L0/L1 wake admission daemon-side into `AttentionGate`; A3 confirmed the
`alias_provider` is built kernel-side from pack seeds + daemon config floor
+ learned tokens — the bridge never participates). A5 removes both fields
from `BridgeConfig` and stops `_load_bridge_config` from reading them.

Per the plan's constraint: `_load_bridge_config` must NOT reject a
`bridge.toml` that still has a leftover `name_aliases`/`always_wake_channels`
key (users may have an old file lying around) — it just stops reading them.
`tomllib.load` + `dict.get`/indexing on the fields we DO want means extra
keys are silently ignored; no strict-key validation is added (spec: no
fallback logic, but this isn't a fallback — it's just not reading a key that
no longer maps to any field).
"""
from __future__ import annotations

from pathlib import Path

from dollos.discord_bridge.__main__ import _load_bridge_config
from dollos.discord_bridge.controller import BridgeConfig


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bridge.toml"
    path.write_text(body)
    return path


def test_load_bridge_config_without_name_aliases_or_always_wake(tmp_path: Path):
    """A bridge.toml with neither dead key loads fine — the common case
    post-A5 (bridge.example.toml no longer has them)."""
    path = _write(
        tmp_path,
        """
[discord]
token = "tok-1"
owner_discord_id = "111"
channel_allowlist = ["c1", "c2"]
""",
    )
    token, cfg = _load_bridge_config(path)
    assert token == "tok-1"
    assert isinstance(cfg, BridgeConfig)
    assert cfg.owner_id == "111"
    assert cfg.channel_allowlist == ["c1", "c2"]
    assert not hasattr(cfg, "name_aliases")
    assert not hasattr(cfg, "always_wake_channels")


def test_load_bridge_config_ignores_leftover_dead_keys(tmp_path: Path):
    """A bridge.toml carrying an OLD `name_aliases` / `always_wake_channels`
    key (pre-A5 file a user hasn't regenerated) must not crash the loader —
    the keys are just never read, not validated against."""
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
    assert cfg.channel_allowlist == ["c1"]
