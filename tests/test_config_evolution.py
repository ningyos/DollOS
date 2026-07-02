"""[evolution] config section (spec §3.6)."""
import tomllib

from dollos.config import EvolutionConfig, Settings

_MIN = """
[llm]
base_url = "http://localhost:8001"
model_alias = "test"
[character]
pack = "packs/gura"
"""


def test_evolution_defaults():
    s = Settings.model_validate(tomllib.loads(_MIN))
    e = s.evolution
    assert e.enabled is True
    assert e.current_self_max_chars == 600
    assert e.current_self_min_chars == 80
    assert e.base_interval_days == 7.0
    assert e.max_interval_days == 28.0
    assert e.idle_threshold_s == 600
    assert e.min_history_events == 8
    assert e.min_diary_days == 14
    assert e.pending_max_surfacings == 5
    assert e.pending_min_age_days == 2.0


def test_evolution_override():
    toml = _MIN + '\n[evolution]\nenabled = false\ncurrent_self_max_chars = 400\n'
    s = Settings.model_validate(tomllib.loads(toml))
    assert s.evolution.enabled is False
    assert s.evolution.current_self_max_chars == 400


def test_evolution_rejects_unknown_key():
    import pytest
    from pydantic import ValidationError
    toml = _MIN + '\n[evolution]\nbogus = 1\n'
    with pytest.raises(ValidationError):
        Settings.model_validate(tomllib.loads(toml))
