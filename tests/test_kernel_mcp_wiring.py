"""Kernel <-> ServiceSupervisor MCP wiring (P1 Task 5). Unit-only: never
spawns a subprocess. Mirrors tests/test_kernel_bridge_wiring.py."""
from pathlib import Path

from dollos.config import (
    CharacterConfig,
    DataConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    McpConfig,
    MemsearchConfig,
    Settings,
)
from dollos.kernel import DollOS


def _make_settings(tmp_path: Path, *, mcp: McpConfig | None = None) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir(exist_ok=True)
    (pack_dir / "doll.toml").write_text(
        '[meta]\nid = "doll"\nname = "Doll"\n\n'
        '[identity]\nself = "You are Doll."\n'
        'personality = "- chill"\ntaboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(provider="llamacpp", template="qwen3-thinking",
                      base_url="http://test.local:8001", model_alias="big"),
        ipc=IPCConfig(host="127.0.0.1", port=9876),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        mcp=mcp if mcp is not None else McpConfig(),
    )


def test_build_mcp_spec_argv(tmp_path):
    cfg_path = tmp_path / "mcp.toml"
    cfg_path.write_text('[server]\nbind_host="127.0.0.1"\nbind_port=9877\n')
    settings = _make_settings(tmp_path, mcp=McpConfig(enabled=True, config=cfg_path))
    dollos = DollOS(settings)
    spec = dollos._build_mcp_spec(settings)
    assert spec.name == "mcp-server"
    assert "-m" in spec.argv and "dollos.mcp_server" in spec.argv
    assert "--config" in spec.argv
    assert str(cfg_path.resolve()) in spec.argv
    assert spec.on_gave_up == dollos._emit_mcp_down_perception


def test_emit_mcp_down_enqueues_perception(tmp_path):
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    dollos._emit_mcp_down_perception("mcp-server", 3)
    # PerceptionQueue wraps an internal asyncio.Queue; reach into it directly
    # since this is a sync test and drain() is async (mirrors
    # tests/test_kernel_bridge_wiring.py:187).
    p = dollos._perception_queue._queue.get_nowait()
    assert p.kind == "McpDown"
    assert p.data["service"] == "mcp-server"
    assert p.data["rc"] == 3


def test_mcp_registered_only_when_enabled_and_config_exists(tmp_path):
    cfg_path = tmp_path / "mcp.toml"
    cfg_path.write_text('[server]\nbind_host="127.0.0.1"\nbind_port=9877\n')
    # enabled + config exists → registered
    s_on = _make_settings(tmp_path, mcp=McpConfig(enabled=True, config=cfg_path))
    d_on = DollOS(s_on)
    names_on = {st["name"] for st in d_on.service_supervisor.status()}
    assert "mcp-server" in names_on
    # default (disabled) → not registered
    d_off = DollOS(_make_settings(tmp_path))
    names_off = {st["name"] for st in d_off.service_supervisor.status()}
    assert "mcp-server" not in names_off
