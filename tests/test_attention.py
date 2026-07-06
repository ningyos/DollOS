"""AttentionGate L0 hard-rule admit + session open (P1c Task 1).

L0 signal logic is moved daemon-side from `discord_bridge/wake.py::l0_wake`,
minus the self-filter (the bridge no longer forwards self-authored events —
see Task 3). `AttentionGate` is pure logic: no I/O, no async, flow-agnostic.
"""
from dollos.mind.attention import AttentionGate, AdmitDecision, Session


def _gate(
    *,
    alias_provider=lambda: frozenset({"gura", "古拉"}),
    always_wake_channels=(),
    owner_id="owner",
    max_session_turns=6,
    window_base_s=90.0,
    window_decay=0.6,
    debounce_engaged_s=2.0,
    debounce_cold_s=8.0,
) -> AttentionGate:
    return AttentionGate(
        alias_provider=alias_provider,
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
    g = _gate(alias_provider=lambda: frozenset({"gura"}), max_session_turns=6, window_base_s=90.0)
    d = g.admit({"channel_id": "c1", "author_id": "u1", "is_dm": False, "mentioned": True, "content": "hi"}, now=100.0)
    assert d.admit and d.reason == "l0_mention"
    s = g._sessions["c1"]
    assert s.participants == {"u1"} and s.turn_count == 0 and s.window_s == 90.0


def test_l0_name_alias_substring_admits():
    g = _gate(alias_provider=lambda: frozenset({"古拉"}))
    d = g.admit(_e(content="hey 古拉 look"), now=100.0)
    assert d.admit and d.reason == "l0_name"


def test_l0_name_ascii_word_boundary_admits():
    """'hey gura' contains 'gura' as a delimited word -> admits."""
    g = _gate(alias_provider=lambda: frozenset({"gura"}))
    d = g.admit(_e(content="hey gura"), now=100.0)
    assert d.admit and d.reason == "l0_name"


def test_l0_name_ascii_word_boundary_rejects_superstring():
    """Word-boundary hardening (D2): a 2-char/short ASCII alias must NOT
    match as a substring of an unrelated longer word — 'gurapp' must not
    wake her just because it contains 'gura'."""
    g = _gate(alias_provider=lambda: frozenset({"gura"}))
    d = g.admit(_e(content="gurapp released a new update"), now=100.0)
    assert not d.admit and d.reason == "not_admitted"


def test_l0_name_ascii_alias_glued_to_cjk_does_not_admit_accepted_gap():
    """Part A whole-branch review, Minor fix 2: pins the accepted
    word-boundary trade-off. Python's ``\\w``/``\\b`` treat CJK characters
    as word characters, so an ASCII alias glued directly to CJK text with
    no delimiter forms no boundary on that side and does NOT match. This
    is a known, accepted gap (not a bug) — pinned here so a future change
    to the boundary regex is a conscious decision, not a silent
    regression."""
    g = _gate(alias_provider=lambda: frozenset({"gura"}))
    assert not g.admit(_e(content="gura你好"), now=100.0).admit
    assert not g.admit(_e(content="我說gura"), now=101.0).admit
    # ASCII-delimited (comma+space) DOES admit — a boundary exists once a
    # non-\w delimiter separates the alias from the rest of the message.
    d = g.admit(_e(content="gura, 在嗎"), now=102.0)
    assert d.admit and d.reason == "l0_name"


def test_l0_name_cjk_alias_substring_in_cjk_sentence_admits():
    """Counterpart to the accepted-gap test above: a pure-CJK token uses
    substring match (no boundary concept applies — CJK has no whitespace
    to delimit a "word"), so it DOES admit when glued into a CJK
    sentence."""
    g = _gate(alias_provider=lambda: frozenset({"古拉"}))
    d = g.admit(_e(content="古拉在嗎"), now=100.0)
    assert d.admit and d.reason == "l0_name"


def test_l0_name_empty_provider_never_fires_other_signals_unaffected():
    """An empty alias set must never admit via l0_name, but other L0
    signals (mention/dm/reply/always) must be unaffected."""
    g = _gate(alias_provider=lambda: frozenset())
    d = g.admit(_e(content="gura hello there"), now=100.0)
    assert not d.admit and d.reason == "not_admitted"
    d2 = g.admit(_e(is_dm=True, content="anything"), now=101.0)
    assert d2.admit and d2.reason == "l0_dm"


def test_l0_name_cjk_single_char_excluded_by_guard_does_not_admit():
    """AttentionGate itself doesn't re-enforce min-length — that guard is
    the PROVIDER's job (spec I3/D2; kernel.py applies it to seeds/floor,
    name_aliases.passes_alias_guard applies it to learned tokens). Any real
    provider filters through that guard before returning its set, so a
    single-char CJK token like '古' never survives to reach the gate. This
    test wires the shared guard into the test provider to prove the
    end-to-end effect: it does NOT admit."""
    from dollos.mind.name_aliases import passes_alias_guard

    raw_tokens = {"古"}  # 1-char CJK — fails MIN_ALIAS_LEN=2
    guarded = frozenset(t for t in raw_tokens if passes_alias_guard(t))
    assert guarded == frozenset()  # sanity: guard actually dropped it
    g = _gate(alias_provider=lambda: guarded)
    d = g.admit(_e(content="古時候的事"), now=100.0)
    assert not d.admit and d.reason == "not_admitted"


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


def test_l0_hit_resets_budget_and_merges_participants():
    g = _gate(window_base_s=90.0)
    # Prime a session manually as if it had drifted (turn_count advanced, window shrunk).
    g._sessions["c1"] = Session(
        channel_id="c1", participants={"u1", "u2"}, last_activity=50.0, turn_count=4, window_s=10.0,
    )
    d = g.admit(_e(is_dm=True, author_id="u1"), now=300.0)
    assert d.admit and d.reason == "l0_dm"
    s = g._sessions["c1"]
    # Re-mention resets her budget/window but MERGES participants (multi-person:
    # never evict someone she was already engaged with). u1 was already present.
    assert s.participants == {"u1", "u2"}
    assert s.turn_count == 0
    assert s.window_s == 90.0
    assert s.last_activity == 300.0


def test_non_signal_without_session_not_admitted():
    g = _gate(alias_provider=lambda: frozenset({"gura"}))
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
