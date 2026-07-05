"""AttentionGate L1 continuation admit + disengage gate + window decay +
note_reply (P1c Task 2).

Covers the engagement-window core: keep chatting without a re-tag
(anti-跟不上) but force a stop once she hits her consecutive-reply cap
(anti-over-fire). Reset happens ONLY on an L0 re-mention (Task 1); L1
continuation and note_reply must never reset turn_count/window back to
base — they only extend last_activity / decay the window / accumulate
turn_count.
"""
import pytest

from dollos.mind.attention import AttentionGate


def _gate(
    *,
    name_aliases=("gura", "古拉"),
    always_wake_channels=(),
    owner_id="owner",
    max_session_turns=6,
    window_base_s=90.0,
    window_decay=0.6,
    debounce_engaged_s=2.0,
    debounce_cold_s=8.0,
) -> AttentionGate:
    return AttentionGate(
        name_aliases=name_aliases,
        always_wake_channels=always_wake_channels,
        owner_id=owner_id,
        max_session_turns=max_session_turns,
        window_base_s=window_base_s,
        window_decay=window_decay,
        debounce_engaged_s=debounce_engaged_s,
        debounce_cold_s=debounce_cold_s,
    )


def _e(**kw):
    base = dict(channel_id="c1", author_id="u1", is_dm=False, mentioned=False, content="hi")
    base.update(kw)
    return base


def test_continuation_admits_without_tag_then_disengages_at_max_turns():
    g = _gate(name_aliases=["gura"], max_session_turns=2, window_base_s=90.0, window_decay=0.6)
    g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "gura?"}, now=100.0)
    # she replies
    g.note_reply("c1", now=101.0)
    # same participant continues, no tag → admitted
    d = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": False, "content": "and also"}, now=102.0)
    assert d.admit and d.reason == "l1_continuation"
    g.note_reply("c1", now=103.0)  # turn_count now 2 == max → disengage
    d2 = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": False, "content": "you there"}, now=104.0)
    assert not d2.admit  # she stopped; only a re-mention reopens
    d3 = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "gura!"}, now=105.0)
    assert d3.admit and d3.reason == "l0_mention"


def test_bystander_chatter_not_admitted():
    g = _gate(name_aliases=["gura"])
    g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "gura?"}, now=100.0)
    d = g.admit({"channel_id": "c1", "author_id": "stranger", "is_dm": False, "mentioned": False, "content": "unrelated"}, now=101.0)
    assert not d.admit  # non-participant bystander in the same channel
    # bystander must not mutate the existing session's participants/state.
    s = g._sessions["c1"]
    assert s.participants == {"u1"}


def test_continuation_extends_last_activity_but_not_turn_count_or_window():
    g = _gate(window_base_s=90.0, window_decay=0.6, max_session_turns=6)
    g.admit(_e(mentioned=True), now=100.0)
    d = g.admit(_e(mentioned=False, content="more chat"), now=110.0)
    assert d.admit and d.reason == "l1_continuation"
    s = g._sessions["c1"]
    assert s.last_activity == 110.0
    assert s.turn_count == 0
    assert s.window_s == 90.0


def test_reset_only_on_remention_note_reply_never_resets():
    g = _gate(max_session_turns=3, window_base_s=90.0, window_decay=0.5)
    g.admit(_e(mentioned=True), now=100.0)
    g.note_reply("c1", now=101.0)  # turn_count=1, window=45.0
    g.note_reply("c1", now=102.0)  # turn_count=2, window=22.5
    s = g._sessions["c1"]
    assert s.turn_count == 2
    assert s.window_s == pytest.approx(90.0 * 0.5 * 0.5)
    # A fresh L0 mention reopens/resets fully.
    d = g.admit(_e(mentioned=True), now=103.0)
    assert d.admit and d.reason == "l0_mention"
    s2 = g._sessions["c1"]
    assert s2.turn_count == 0
    assert s2.window_s == 90.0


def test_window_decay_after_n_replies():
    base = 90.0
    decay = 0.6
    g = _gate(max_session_turns=100, window_base_s=base, window_decay=decay)
    g.admit(_e(mentioned=True), now=0.0)
    n = 4
    for i in range(n):
        g.note_reply("c1", now=float(i + 1))
    s = g._sessions["c1"]
    assert s.window_s == pytest.approx(base * (decay ** n))
    assert s.turn_count == n


def test_window_expiry_clears_session_and_not_admitted():
    g = _gate(window_base_s=10.0, max_session_turns=6)
    g.admit(_e(mentioned=True), now=100.0)
    # past window_s with no activity
    d = g.admit(_e(mentioned=False, content="still here?"), now=111.0)
    assert not d.admit and d.reason == "not_admitted"
    assert "c1" not in g._sessions


def test_note_reply_disengages_session_at_cap_even_without_intervening_admit():
    g = _gate(max_session_turns=1, window_base_s=90.0, window_decay=0.6)
    g.admit(_e(mentioned=True), now=100.0)
    g.note_reply("c1", now=101.0)  # turn_count now 1 == max → disengage
    assert "c1" not in g._sessions


def test_note_reply_noop_when_no_session():
    g = _gate()
    g.note_reply("nonexistent", now=100.0)  # must not raise


def test_differentiated_debounce_window_for():
    g = _gate(debounce_engaged_s=2.0, debounce_cold_s=8.0, window_base_s=90.0)
    # cold: no session yet
    assert g.window_for("c1", now=100.0) == 8.0
    g.admit(_e(mentioned=True), now=100.0)
    assert g.is_engaged("c1", now=100.0)
    assert g.window_for("c1", now=100.0) == 2.0
    # after expiry, cold again
    assert g.window_for("c1", now=300.0) == 8.0
    assert not g.is_engaged("c1", now=300.0)


def test_is_engaged_false_for_unknown_channel():
    g = _gate()
    assert not g.is_engaged("never-seen", now=100.0)


def test_l0_remention_merges_participants_and_resets_budget():
    """Multi-person: a second author's L0 mention must BROADEN the
    participant set (merge, not replace) and reset her budget/window —
    never evict someone she was already engaged with. Fails against the
    old wholesale-replace behavior (participants would become {B}, and
    A's tagless continuation would be rejected)."""
    g = _gate(name_aliases=["gura"], max_session_turns=6, window_base_s=90.0, window_decay=0.6)
    # A opens the session.
    g.admit({"channel_id": "c1", "author_id": "A", "is_dm": False, "mentioned": True, "content": "gura?"}, now=100.0)
    # She replies a couple times → turn_count>0, window decayed.
    g.note_reply("c1", now=101.0)
    g.note_reply("c1", now=102.0)
    s_before = g._sessions["c1"]
    assert s_before.turn_count == 2 and s_before.window_s < 90.0
    # B L0-mentions in the same channel.
    d = g.admit({"channel_id": "c1", "author_id": "B", "is_dm": False, "mentioned": True, "content": "gura!"}, now=110.0)
    assert d.admit and d.reason == "l0_mention"
    s = g._sessions["c1"]
    assert s.participants == {"A", "B"}  # merged, not replaced
    assert s.turn_count == 0  # re-mention resets her budget
    assert s.window_s == 90.0  # re-mention resets the window
    assert s.last_activity == 110.0
    # A (dropped under the old behavior) can still continue tagless.
    dA = g.admit({"channel_id": "c1", "author_id": "A", "is_dm": False, "mentioned": False, "content": "still me"}, now=111.0)
    assert dA.admit and dA.reason == "l1_continuation"
    # B can also continue tagless.
    dB = g.admit({"channel_id": "c1", "author_id": "B", "is_dm": False, "mentioned": False, "content": "and me"}, now=112.0)
    assert dB.admit and dB.reason == "l1_continuation"
