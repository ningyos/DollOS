# tests/test_evolution_tripwire.py
"""Tamper tripwire orchestrator (spec §5)."""
from dollos.character import Enforcement
from dollos.mind import evolution as evo
from dollos.mind import self_history


def _tp(tmp_path, **kw):
    defaults = dict(
        current_self_path=tmp_path / "current_self.md",
        history_path=tmp_path / "self_history.jsonl",
        slot_path=tmp_path / "self_evolution" / "pending.json",
        enforcement=Enforcement(), floor=80, cap=600, now=1.0)
    defaults.update(kw)
    return evo.process_tripwire(**defaults)


def _kinds(tmp_path):
    return [e["kind"] for e in self_history.read_events(tmp_path / "self_history.jsonl")]


def test_in_sync_no_side_effects(tmp_path):
    # sanctioned == file → nothing.
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="我"*90, old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("我"*90, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path) == ["evo_adopt"]


def test_crash_repair_rewrites_file_no_slot(tmp_path):
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="新版"+"字"*88,
                           old_text="舊版"+"字"*88, drift_score=0.1)
    cs = tmp_path/"current_self.md"
    cs.write_text("舊版"+"字"*88, encoding="utf-8")  # == old_text (log-then-write window)
    _tp(tmp_path)
    assert cs.read_text(encoding="utf-8") == "新版"+"字"*88
    assert _kinds(tmp_path)[-1] == "evo_repair"
    assert not (tmp_path/"self_evolution"/"pending.json").exists()


def test_new_edit_passing_checks_creates_external_slot(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("有人改成這樣"+"字"*88, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path)[-1] == "external_edit"
    slot = evo.load_slot(tmp_path/"self_evolution"/"pending.json")
    assert slot.kind == "external" and slot.status == "awaiting_skeptic"


def test_new_edit_failing_checks_restores_and_no_slot(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    cs = tmp_path/"current_self.md"
    cs.write_text("太短", encoding="utf-8")  # fails floor
    _tp(tmp_path)
    edits = [e for e in self_history.read_events(tmp_path/"self_history.jsonl")
             if e["kind"] == "external_edit"]
    assert edits[-1]["reason"] is not None  # mechanical-fail carries a reason
    assert cs.read_text(encoding="utf-8") == "原版"+"字"*88  # restored
    assert not (tmp_path/"self_evolution"/"pending.json").exists()


def test_edit_while_slot_exists_logs_only(tmp_path):
    self_history.log_event(tmp_path/"self_history.jsonl", kind="evo_adopt",
                           text="原版"+"字"*88, old_text=None, drift_score=None)
    evo.save_slot(tmp_path/"self_evolution"/"pending.json",
                  evo.make_external_slot(candidate="別的候選"+"字"*88, created_ts=0.0))
    cs = tmp_path/"current_self.md"
    cs.write_text("又有人改"+"字"*88, encoding="utf-8")
    _tp(tmp_path)
    assert _kinds(tmp_path)[-1] == "external_edit"
    # Slot NOT replaced — external edits are not queued for auto-promotion.
    assert evo.load_slot(tmp_path/"self_evolution"/"pending.json").candidate == "別的候選"+"字"*88
    # On-disk file untouched until slot resolution (slot-resolution invariant owns it).
    assert cs.read_text(encoding="utf-8") == "又有人改"+"字"*88


def test_crash_repair_log_failure_leaves_file_divergent(tmp_path, monkeypatch):
    """Log-before-mutate: a failed evo_repair append must leave the file
    divergent so the next turn retries — no permanent audit gap."""
    import pytest
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="新版"+"字"*88,
                           old_text="舊版"+"字"*88, drift_score=0.1)
    cs = tmp_path/"current_self.md"
    cs.write_text("舊版"+"字"*88, encoding="utf-8")  # crash_repair state

    def boom(path, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(self_history, "log_event", boom)
    with pytest.raises(OSError):
        _tp(tmp_path)
    assert cs.read_text(encoding="utf-8") == "舊版"+"字"*88  # NOT restored
    monkeypatch.undo()
    assert "evo_repair" not in _kinds(tmp_path)  # no audit line either


def test_stranded_edit_completes_slot_creation(tmp_path):
    """Bounded completion rule: external_edit logged but slot creation crashed
    → next tripwire pass completes the slot with NO duplicate log line."""
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="原版"+"字"*88,
                           old_text=None, drift_score=None)
    edited = "有人改成這樣"+"字"*88
    # Simulate the crash window: the edit is logged, but no slot landed.
    self_history.log_event(hist, kind="external_edit", text=edited, reason=None)
    (tmp_path/"current_self.md").write_text(edited, encoding="utf-8")
    n_before = len(self_history.read_events(hist))
    _tp(tmp_path)  # classifies already_logged → completion rule fires
    slot = evo.load_slot(tmp_path/"self_evolution"/"pending.json")
    assert slot is not None and slot.kind == "external" and slot.status == "awaiting_skeptic"
    assert slot.candidate == edited
    assert len(self_history.read_events(hist)) == n_before  # no duplicate external_edit


def test_unchanged_divergent_no_spam(tmp_path):
    hist = tmp_path/"self_history.jsonl"
    self_history.log_event(hist, kind="evo_adopt", text="原版"+"字"*88,
                           old_text=None, drift_score=None)
    (tmp_path/"current_self.md").write_text("有人改"+"字"*88, encoding="utf-8")
    _tp(tmp_path)  # first detection → external_edit + slot
    n1 = len(self_history.read_events(hist))
    _tp(tmp_path)  # second turn, same file → no new log
    assert len(self_history.read_events(hist)) == n1


def test_bootstrap_new_edit_restore_is_delete(tmp_path):
    cs = tmp_path/"current_self.md"
    cs.write_text("太短", encoding="utf-8")  # no sanctioned predecessor, fails floor
    _tp(tmp_path)
    assert not cs.exists()  # bootstrap restore = delete
