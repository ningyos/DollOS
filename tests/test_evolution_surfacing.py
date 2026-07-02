"""[人格演化候選] surfacing + expiry (spec §3.4)."""
from dollos.mind import evolution as evo
from dollos.mind import self_history


def test_render_surfacing_keeper_has_all_load_bearing_parts():
    slot = evo.make_keeper_slot(candidate="我現在監控數字時會主動來勁。", rationale="活很久的 pin",
                                hwm_before=None, created_ts=1.0)
    out = evo.render_surfacing(slot=slot, sanctioned_text="我以前沒事就安靜待著。", reminder_n=1)
    assert "[人格演化候選]" in out
    assert "我以前沒事就安靜待著。" in out and "我現在監控數字時會主動來勁。" in out
    assert "活很久的 pin" in out            # rationale (keeper)
    assert "adopt" in out and "reject" in out  # operational hint
    assert "採不採納由妳" in out or "由妳" in out  # 主權句
    assert "第 1 次" in out                  # 第N次提醒


def test_render_surfacing_external_uses_neutral_attribution():
    slot = evo.make_external_slot(candidate="有人手動改的內容。", created_ts=1.0)
    slot.status = "awaiting_doll"
    out = evo.render_surfacing(slot=slot, sanctioned_text=None, reminder_n=2)
    # Neutral attribution — never "可能是主人" (spec §3.4).
    assert "無法確認是誰" in out
    assert "可能是主人" not in out


def test_render_surfacing_counter_kill_notice_leads():
    base = evo.make_keeper_slot(candidate="原候選內容。", rationale="R", hwm_before=None, created_ts=1.0)
    c = evo.to_counter(base, new_text="我的改寫。", created_ts_now=2.0)
    reverted = evo.revert_to_fallback(c, reason="牴觸 taboo")
    out = evo.render_surfacing(slot=reverted, sanctioned_text=None, reminder_n=1)
    assert "未通過" in out and "牴觸 taboo" in out and "原候選內容。" in out


def test_surface_increments_count(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_keeper_slot(candidate="x"*90, rationale="R",
                                           hwm_before=None, created_ts=0.0))
    block = evo.surface_or_expire(
        slot_path=sp, history_path=tmp_path / "self_history.jsonl",
        current_self_path=tmp_path / "current_self.md", sanctioned_text=None,
        max_surfacings=5, min_age_days=2.0, now=1.0)
    assert block is not None
    assert evo.load_slot(sp).surfaced_count == 1


def test_surface_awaiting_skeptic_returns_none(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="x"*90, created_ts=0.0))
    block = evo.surface_or_expire(
        slot_path=sp, history_path=tmp_path / "self_history.jsonl",
        current_self_path=tmp_path / "current_self.md", sanctioned_text=None,
        max_surfacings=5, min_age_days=2.0, now=1.0)
    assert block is None
    assert evo.load_slot(sp).surfaced_count == 0  # awaiting_skeptic never increments


def test_expiry_needs_count_and_age(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    slot = evo.make_keeper_slot(candidate="x"*90, rationale="R", hwm_before=None, created_ts=0.0)
    slot.surfaced_count = 5  # count threshold met
    evo.save_slot(sp, slot)
    day = 86400.0
    # Age NOT met (now < min_age_days) → still surfaces, not expired.
    block = evo.surface_or_expire(slot_path=sp, history_path=hist,
                                  current_self_path=tmp_path/"current_self.md",
                                  sanctioned_text=None, max_surfacings=5,
                                  min_age_days=2.0, now=1.0)
    assert block is not None and evo.load_slot(sp) is not None
    # Age met too → expires.
    block = evo.surface_or_expire(slot_path=sp, history_path=hist,
                                  current_self_path=tmp_path/"current_self.md",
                                  sanctioned_text=None, max_surfacings=5,
                                  min_age_days=2.0, now=3 * day)
    assert block is None
    assert evo.load_slot(sp) is None
    assert self_history.read_events(hist)[-1]["kind"] == "evo_expire"


def test_expiry_restores_divergent_file(tmp_path):
    sp = tmp_path / "self_evolution" / "pending.json"
    hist = tmp_path / "self_history.jsonl"
    cs = tmp_path / "current_self.md"
    cs.write_text("有人手動改的", encoding="utf-8")
    slot = evo.make_external_slot(candidate="有人手動改的", created_ts=0.0)
    slot.status = "awaiting_doll"
    slot.surfaced_count = 5
    evo.save_slot(sp, slot)
    evo.surface_or_expire(slot_path=sp, history_path=hist, current_self_path=cs,
                          sanctioned_text=None, max_surfacings=5, min_age_days=0.0,
                          now=1.0)
    assert not cs.exists()  # slot-resolution invariant: bootstrap restore = delete
