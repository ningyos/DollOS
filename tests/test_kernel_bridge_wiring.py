"""Tests for kernel <-> ServiceSupervisor bridge wiring (svc-internalization Task C).

Unit-only: never spawns a real subprocess. Covers:
- `_derive_daemon_ws` (module-level WS-URL derivation helper)
- `_build_bridge_spec` (argv/ServiceSpec construction on the DollOS kernel)

Registration-gating (enabled + config-exists) and the start()/stop() wiring
itself are exercised structurally via `DollOS.__init__` (register-or-not) and
are NOT re-verified by spawning anything — that's the human live-smoke
(brief Step 10).
"""
from pathlib import Path

from dollos.config import (
    BridgeConfig,
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS, _derive_daemon_ws
from dollos.service_supervisor import ServiceSpec


def _make_settings(tmp_path: Path, *, bridge: BridgeConfig | None = None) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
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
        ipc=IPCConfig(host="127.0.0.1", port=9876),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        bridge=bridge if bridge is not None else BridgeConfig(),
    )


# ----- _derive_daemon_ws -----


def test_derive_daemon_ws_loopback_for_wildcard():
    class _IPC:
        host, port = "0.0.0.0", 9876

    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9876"


def test_derive_daemon_ws_loopback_for_ipv6_any():
    class _IPC:
        host, port = "::", 9876

    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9876"


def test_derive_daemon_ws_loopback_for_empty_string():
    class _IPC:
        host, port = "", 9876

    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9876"


def test_derive_daemon_ws_keeps_explicit_host():
    class _IPC:
        host, port = "127.0.0.1", 9999

    assert _derive_daemon_ws(_IPC()) == "ws://127.0.0.1:9999"


def test_derive_daemon_ws_brackets_bare_ipv6_literal():
    """P1g whole-branch review minor #3: an explicit (non-wildcard) IPv6
    literal like `::1` must be bracketed — `ws://::1:9876` is unparseable
    (the colons collide with the URL's own host:port separator)."""
    class _IPC:
        host, port = "::1", 9876

    assert _derive_daemon_ws(_IPC()) == "ws://[::1]:9876"


# ----- _build_bridge_spec -----


def test_build_bridge_spec_argv(tmp_path: Path) -> None:
    bridge_toml = tmp_path / "bridge.toml"
    bridge_toml.write_text("[discord]\ntoken = \"fake\"\nowner_discord_id = \"1\"\n")
    settings = _make_settings(
        tmp_path, bridge=BridgeConfig(enabled=True, config=bridge_toml)
    )
    kernel = DollOS(settings)

    spec = kernel._build_bridge_spec(settings)

    assert isinstance(spec, ServiceSpec)
    assert spec.name == "discord-bridge"
    assert "dollos.discord_bridge" in spec.argv
    assert str(bridge_toml.resolve()) in spec.argv
    assert str(settings.data.root.resolve()) in spec.argv
    assert "--retention-days" in spec.argv
    assert "--config" in spec.argv
    assert "--data-root" in spec.argv
    assert "--daemon" in spec.argv
    assert "ws://127.0.0.1:9876" in spec.argv
    # token must never leak into argv (only the config *path* does)
    assert not any("fake" in a or "token" in a.lower() for a in spec.argv)
    # on_gave_up must be bound to the kernel's perception emitter
    assert spec.on_gave_up == kernel._emit_bridge_down_perception


def test_build_bridge_spec_retention_days_value(tmp_path: Path) -> None:
    from dollos.service_supervisor import _RETENTION_DAYS

    bridge_toml = tmp_path / "bridge.toml"
    bridge_toml.write_text("[discord]\ntoken = \"fake\"\nowner_discord_id = \"1\"\n")
    settings = _make_settings(
        tmp_path, bridge=BridgeConfig(enabled=True, config=bridge_toml)
    )
    kernel = DollOS(settings)

    spec = kernel._build_bridge_spec(settings)
    idx = spec.argv.index("--retention-days")
    assert spec.argv[idx + 1] == str(_RETENTION_DAYS)


# ----- registration gating (enabled + config-exists) -----


def test_kernel_registers_bridge_when_enabled_and_config_exists(tmp_path: Path) -> None:
    bridge_toml = tmp_path / "bridge.toml"
    bridge_toml.write_text("[discord]\ntoken = \"fake\"\nowner_discord_id = \"1\"\n")
    settings = _make_settings(
        tmp_path, bridge=BridgeConfig(enabled=True, config=bridge_toml)
    )
    kernel = DollOS(settings)

    statuses = kernel.service_supervisor.status()
    assert any(s["name"] == "discord-bridge" for s in statuses)


def test_kernel_does_not_register_bridge_when_disabled(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, bridge=BridgeConfig(enabled=False))
    kernel = DollOS(settings)

    statuses = kernel.service_supervisor.status()
    assert not any(s["name"] == "discord-bridge" for s in statuses)


def test_kernel_does_not_register_bridge_when_config_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    settings = _make_settings(
        tmp_path, bridge=BridgeConfig(enabled=True, config=missing)
    )
    kernel = DollOS(settings)

    statuses = kernel.service_supervisor.status()
    assert not any(s["name"] == "discord-bridge" for s in statuses)


# ----- BridgeDown perception emission -----


def test_emit_bridge_down_perception_enqueues_perception(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    kernel = DollOS(settings)

    kernel._emit_bridge_down_perception("discord-bridge", 1)

    # PerceptionQueue wraps an internal asyncio.Queue; reach into it directly
    # since this is a sync test and drain() is async.
    perception = kernel._perception_queue._queue.get_nowait()
    assert perception.kind == "BridgeDown"
    assert perception.data == {"service": "discord-bridge", "rc": 1}
