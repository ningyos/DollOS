"""Pending-slot schema + lifecycle state machine (spec §3.4)."""
import json

from dollos.mind import evolution as evo


def test_constants():
    assert evo.COUNTER_ROUND_CAP == 2
    assert evo.VERDICT_ERRORS_BOUND == 3
    assert evo.ECHO_SIMILARITY == 0.9


def test_keeper_slot_enters_awaiting_doll():
    s = evo.make_keeper_slot(candidate="候選文", rationale="因為X",
                             hwm_before=128, created_ts=100.0)
    assert s.kind == "keeper" and s.status == "awaiting_doll"
    assert s.candidate == "候選文" and s.hwm_before == 128
    assert s.counter_round == 0 and s.surfaced_count == 0 and s.verdict_errors == 0
    assert s.fallback is None


def test_external_slot_enters_awaiting_skeptic():
    s = evo.make_external_slot(candidate="有人改的", created_ts=100.0)
    assert s.kind == "external" and s.status == "awaiting_skeptic"
    assert s.hwm_before is None  # external consumed no evidence window
    assert s.last_error_ts is None  # no skeptic error yet (I3 cooldown field)


def test_to_counter_replaces_and_bumps_round_resets_surface():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    base.surfaced_count = 3
    base = evo.mark_awaiting_doll(base)  # keeper already awaiting_doll; idempotent
    c = evo.to_counter(base, new_text="我的改寫", created_ts_now=200.0)
    assert c.kind == "counter" and c.status == "awaiting_skeptic"
    assert c.candidate == "我的改寫"
    assert c.counter_round == 1
    assert c.surfaced_count == 0                      # reset (spec §3.4 R3′)
    assert c.created_ts == 100.0 and c.hwm_before == 5  # inherited from originator
    assert c.fallback == {"candidate": "原候選", "rationale": "R", "kind": "keeper"}


def test_to_counter_second_round_carries_fallback_forward():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    c1 = evo.to_counter(base, new_text="改寫1", created_ts_now=200.0)
    c1 = evo.mark_awaiting_doll(c1)
    c2 = evo.to_counter(c1, new_text="改寫2", created_ts_now=300.0)
    assert c2.counter_round == 2
    assert c2.fallback == {"candidate": "改寫1", "rationale": None, "kind": "counter"}


def test_revert_to_fallback_sets_notice_and_awaiting_doll():
    base = evo.make_keeper_slot(candidate="原候選", rationale="R",
                                hwm_before=5, created_ts=100.0)
    c = evo.to_counter(base, new_text="改寫", created_ts_now=200.0)
    reverted = evo.revert_to_fallback(c, reason="牴觸 taboo")
    assert reverted.status == "awaiting_doll"
    assert reverted.kind == "keeper" and reverted.candidate == "原候選"
    assert reverted.rationale == "R"
    assert reverted.notice == "牴觸 taboo"
    assert reverted.counter_round == 1  # bound accounting preserved


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "self_evolution" / "pending.json"
    s = evo.make_external_slot(candidate="有人改的", created_ts=100.0)
    s.surfaced_count = 2
    evo.save_slot(p, s)
    loaded = evo.load_slot(p)
    assert loaded == s


def test_load_missing_slot_is_none(tmp_path):
    assert evo.load_slot(tmp_path / "pending.json") is None


def test_corrupt_slot_quarantined_and_none_with_audit_line(tmp_path):
    from dollos.mind import self_history
    p = tmp_path / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    p.write_text("{not json", encoding="utf-8")
    assert evo.load_slot(p, history_path=hist) is None
    assert (tmp_path / "pending.json.corrupt").exists()
    assert not p.exists()
    # The spec-promised evo_error audit line (spec §3.4, review M4).
    assert self_history.read_events(hist)[-1]["kind"] == "evo_error"


def test_non_dict_json_quarantined(tmp_path):
    from dollos.mind import self_history
    p = tmp_path / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    for bad in ('null', '[]', '"hi"', '42'):
        p.write_text(bad, encoding="utf-8")
        assert evo.load_slot(p, history_path=hist) is None
        assert not p.exists()
        (tmp_path / "pending.json.corrupt").unlink()  # reset for next round
    events = self_history.read_events(hist)
    assert len(events) == 4 and all(e["kind"] == "evo_error" for e in events)


def test_clear_slot_idempotent(tmp_path):
    p = tmp_path / "pending.json"
    evo.clear_slot(p)  # no-op on absent
    evo.save_slot(p, evo.make_external_slot(candidate="x", created_ts=1.0))
    evo.clear_slot(p)
    assert not p.exists()
