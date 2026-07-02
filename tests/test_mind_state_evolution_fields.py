"""Plan-3 MindState fields: explicit save/load round-trip (house discipline)."""
from dollos.mind.mind_state import MindState, load_state, save_state


def test_defaults():
    s = MindState()
    assert s.last_evolution_attempt_at == 0.0
    assert s.evolution_interval_days == 0.0
    assert s.evolution_hwm == 0


def test_round_trip(tmp_path):
    p = tmp_path / "mind_state.json"
    s = MindState()
    s.last_evolution_attempt_at = 1234.5
    s.evolution_interval_days = 14.0
    s.evolution_hwm = 4096
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.last_evolution_attempt_at == 1234.5
    assert loaded.evolution_interval_days == 14.0
    assert loaded.evolution_hwm == 4096


def test_load_clamps_negative_hwm(tmp_path):
    import json
    p = tmp_path / "mind_state.json"
    s = MindState()
    save_state(s, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["evolution_hwm"] = -5
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_state(p).evolution_hwm == 0


def test_load_missing_fields_defaults(tmp_path):
    """A pre-Plan-3 state file (fields absent) loads with defaults."""
    import json
    p = tmp_path / "mind_state.json"
    s = MindState()
    save_state(s, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    for k in ("last_evolution_attempt_at", "evolution_interval_days", "evolution_hwm"):
        data.pop(k, None)
    p.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(p)
    assert loaded.last_evolution_attempt_at == 0.0
    assert loaded.evolution_interval_days == 0.0
    assert loaded.evolution_hwm == 0
