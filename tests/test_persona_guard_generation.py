"""Generation-aware baselines (spec §3.5)."""
from dollos.mind.persona_guard import (
    append_baseline,
    baselines_for_generation,
    load_baselines,
)


def test_append_stamps_generation(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "gen0 response", generation=0)
    append_baseline(p, "k", "gen1 response", generation=1)
    recs = load_baselines(p)["k"]
    assert [r["generation"] for r in recs] == [0, 1]
    assert [r["response"] for r in recs] == ["gen0 response", "gen1 response"]


def test_default_generation_is_zero(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "legacy")
    assert load_baselines(p)["k"][0]["generation"] == 0


def test_legacy_record_without_generation_reads_as_zero(tmp_path):
    p = tmp_path / "gura.jsonl"
    p.write_text('{"prompt_key": "k", "response": "old", "fingerprint": "x", "ts": 1}\n',
                 encoding="utf-8")
    assert load_baselines(p)["k"][0]["generation"] == 0


def test_baselines_for_generation_filters(tmp_path):
    p = tmp_path / "gura.jsonl"
    append_baseline(p, "k", "a", generation=0)
    append_baseline(p, "k", "b", generation=1)
    append_baseline(p, "k", "c", generation=1)
    cur = baselines_for_generation(load_baselines(p), 1)
    assert cur == {"k": ["b", "c"]}
    assert baselines_for_generation(load_baselines(p), 2) == {}  # empty current pool
