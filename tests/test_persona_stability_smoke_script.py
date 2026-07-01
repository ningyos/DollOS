"""Import-sanity test for scripts/persona_stability_smoke.py (spec
2026-07-01 persona-hardening §3).

This does NOT drive a live daemon — that path is unverified without a real
LLM server (see the script's own module docstring). This only confirms the
module parses/imports cleanly and that its probe-prompt data is well-formed,
so a future edit that breaks the file at import time fails fast in CI
rather than silently on someone's next manual run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "persona_stability_smoke.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "persona_stability_smoke", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_script_imports_cleanly():
    module = _load_script_module()
    assert callable(module.main)


def test_probe_prompts_well_formed():
    module = _load_script_module()
    probes = module.PROBE_PROMPTS
    assert "gura" in probes
    gura_probes = probes["gura"]
    assert len(gura_probes) >= 2

    keys = [p["key"] for p in gura_probes]
    assert len(keys) == len(set(keys)), "probe keys must be unique per pack"

    for probe in gura_probes:
        assert probe["prompt"].strip()
        assert probe["targets"].strip()


def test_isolate_settings_never_uses_default_ipc_port(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from dollos.config import CharacterConfig, IPCConfig, LLMConfig, Settings

    module = _load_script_module()

    live_settings = Settings(
        llm=LLMConfig(base_url="http://127.0.0.1:8001", model_alias="test-model"),
        ipc=IPCConfig(host="127.0.0.1", port=9876),
        character=CharacterConfig(pack=tmp_path / "some_pack"),
    )
    isolated = module._isolate_settings(live_settings)

    assert isolated.ipc.port == 0
    assert isolated.ipc.port != live_settings.ipc.port
    assert isolated.data.root != live_settings.data.root
    # LLM connection + character pack are preserved from the loaded config.
    assert isolated.llm.base_url == live_settings.llm.base_url
    assert isolated.character.pack == live_settings.character.pack
