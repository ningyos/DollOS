"""Tests for the [Self profile] block (Task 6): on every page snapshot
(render_epoch_start) — again in a turn's message only when it changed."""
from dollos.mind.mind_prompt import Seen, render_epoch_start, _percep_body
from dollos.mind.mind_state import MindState, Perception
import time

NOW = 1_790_000_000.0


def _state():
    return MindState()  # 若 MindState 需參數,沿用既有測試建法


def test_self_profile_block_opens_the_snapshot():
    body = "## What I've learned about myself\n- [s1·2026-06-30] 我重視誠實"
    out = render_epoch_start(_state(), Seen(), "internal", NOW, self_profile_text=body)
    assert "[Self profile]" in out
    assert body in out
    # 位置:core self 最先 —— 在 [Mind state] 之前
    assert out.index("[Self profile]") < out.index("[Mind state]")


def test_self_profile_block_absent_when_none():
    out = render_epoch_start(_state(), Seen(), "internal", NOW, self_profile_text=None)
    assert "[Self profile]" not in out


def test_reflection_nudge_mentions_pinself():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "PinSelf" in text
    assert "yourself" in text


def test_reflection_nudge_grounds_before_introspection():
    """Anti-performativity guard: the grounding clause ('look back at what you actually did')
    must come before any invitation to introspect, so a weak model doesn't
    free-associate ungrounded self-description."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert text.index("what you actually did") < text.index("PinSelf")


def test_reflection_nudge_states_subject_test():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert 'the subject is "you" → PinSelf' in text


def test_reflection_nudge_allows_write_loose_not_yet_durable():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "record it anyway" in text
    assert "pruning later does the filtering" in text


def test_reflection_nudge_keeps_bootstrap_safe_clause():
    """Regression guard for commit b9d87b7 — without this clause the model
    waits for a non-empty [Self profile] before its first pin."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "no need to wait until [Self profile] already has entries" in text


def test_reflection_nudge_keeps_operational_calling_hint():
    """Regression guard for commit 70de95a — without the explicit
    (op=add, section one of ...) hint, nudge salience regresses."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "PinSelf (op=add, section one of self/relationship/user)" in text


def test_reflection_nudge_states_notetoollesson_division():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "NoteToolLesson" in text


def test_reflection_nudge_states_selfrevision_division_of_labor():
    """Live-smoke root cause (p2-live-smoke-report.md): the ReflectionMoment
    nudge was 100% PinSelf-focused, so the model conflated [Self revision candidate]
    with PinSelf's self-profile machinery. A division-of-labor sentence
    naming SelfRevision must appear, without disturbing the load-bearing
    PinSelf content above it."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "[Self revision candidate]" in text
    assert "SelfRevision" in text
    assert "PinSelf can't handle it" in text
    # Existing load-bearing PinSelf content must come first (appended after).
    assert text.index("NoteToolLesson") < text.index("SelfRevision")


def test_reflection_nudge_is_english_and_names_the_candidate_block_like_surfacing():
    """Plan 2026-10-19 Task 5: the nudge is the daemon's words — English — and
    names the candidate block exactly as render_surfacing titles it."""
    from dollos.mind import evolution as evo
    from tests._cjk import cjk_runs
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert cjk_runs(text) == []
    assert text.startswith("(time to reflect (5 iters since last). ")
    slot = evo.make_keeper_slot(candidate="c", rationale="r", hwm_before=None, created_ts=1.0)
    header = evo.render_surfacing(slot=slot, sanctioned_text="s", reminder_n=1).splitlines()[0]
    assert header == "[Self revision candidate]" and header in text


def test_self_profile_header_frames_prune_as_selection():
    out = render_epoch_start(
        _state(), Seen(), "internal", NOW, self_profile_text="- [s1·2026-07-02] test"
    )
    assert "keep only what's still you" in out
