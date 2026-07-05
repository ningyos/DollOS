"""AttentionGate L0 hard-rule admit + session open (P1c Task 1).

L0 signal logic is moved daemon-side from `discord_bridge/wake.py::l0_wake`,
minus the self-filter (the bridge no longer forwards self-authored events —
see Task 3). `AttentionGate` is pure logic: no I/O, no async, flow-agnostic.
"""
from dollos.mind.attention import AttentionGate, AdmitDecision, Session


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


def test_l0_dm_admits_and_opens_session():
    g = _gate()
    d = g.admit(_e(is_dm=True), now=100.0)
    assert d.admit and d.reason == "l0_dm"
    s = g._sessions["c1"]
    assert s.participants == {"u1"} and s.turn_count == 0 and s.window_s == 90.0


def test_l0_mention_admits_and_opens_session():
    g = _gate(name_aliases=["gura"], max_session_turns=6, window_base_s=90.0)
    d = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "hi"}, now=100.0)
    assert d.admit and d.reason == "l0_mention"
    s = g._sessions["c1"]
    assert s.participants == {"u1"} and s.turn_count == 0 and s.window_s == 90.0


def test_l0_name_alias_substring_admits():
    g = _gate(name_aliases=["古拉"])
    d = g.admit(_e(content="hey 古拉 look"), now=100.0)
    assert d.admit and d.reason == "l0_name"


def test_l0_reply_to_bot_admits():
    g = _gate()
    d = g.admit(_e(content="unrelated", reply_to_bot=True), now=100.0)
    assert d.admit and d.reason == "l0_reply"


def test_l0_always_wake_channel_admits():
    g = _gate(always_wake_channels={"vip"})
    d = g.admit(_e(channel_id="vip", content="unrelated"), now=100.0)
    assert d.admit and d.reason == "l0_always"


def test_l0_opens_session_with_correct_participant_and_window():
    g = _gate(window_base_s=42.0)
    d = g.admit(_e(is_dm=True, author_id="u9"), now=200.0)
    assert d.admit
    s = g._sessions["c1"]
    assert s.channel_id == "c1"
    assert s.participants == {"u9"}
    assert s.last_activity == 200.0
    assert s.turn_count == 0
    assert s.window_s == 42.0


def test_l0_hit_resets_existing_session():
    g = _gate(window_base_s=90.0)
    # Prime a session manually as if it had drifted (turn_count advanced, window shrunk).
    g._sessions["c1"] = Session(
        channel_id="c1", participants={"u1", "u2"}, last_activity=50.0, turn_count=4, window_s=10.0,
    )
    d = g.admit(_e(is_dm=True, author_id="u1"), now=300.0)
    assert d.admit and d.reason == "l0_dm"
    s = g._sessions["c1"]
    assert s.participants == {"u1"}
    assert s.turn_count == 0
    assert s.window_s == 90.0
    assert s.last_activity == 300.0


def test_non_signal_without_session_not_admitted():
    g = _gate(name_aliases=["gura"])
    d = g.admit({"channel_id": "c1", "author_id": "u2", "is_dm": False, "mentioned": False, "content": "unrelated chatter"}, now=100.0)
    assert not d.admit and d.reason == "not_admitted"
    assert "c1" not in g._sessions


def test_reply_to_bot_absent_key_is_safe_falsy_default():
    """Event dicts without a reply_to_bot key (bridge hasn't been wired yet,
    Task 3 concern) must not admit — `.get` defaults to falsy."""
    g = _gate()
    d = g.admit(_e(content="no reply flag at all"), now=100.0)
    assert not d.admit and d.reason == "not_admitted"


def test_admit_decision_and_session_are_dataclasses_with_expected_fields():
    d = AdmitDecision(admit=True, reason="l0_dm")
    assert d.admit is True and d.reason == "l0_dm"
    s = Session(channel_id="c1", participants={"u1"}, last_activity=1.0, turn_count=0, window_s=90.0)
    assert s.channel_id == "c1" and s.participants == {"u1"}
    assert s.turn_count == 0 and s.window_s == 90.0
