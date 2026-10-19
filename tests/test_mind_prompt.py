"""Tests for the MindLoop prompt renderer's blocks.

The whole-prompt ``render_mind`` is gone (plan 2026-10-18 Task 9): a
conversation opens a page with ``render_epoch_start`` and adds one
``render_turn_delta`` per turn, so the blocks are checked through those
(their own composition tests live in tests/test_turn_delta.py).
"""
import time

from dollos.mind.mind_state import (
    MindState, OpenLoop,
    Perception, OutputRecord,
)
from dollos.mind.mind_prompt import (
    Seen,
    render_epoch_start,
    render_memory_guideline,
    render_turn_delta,
)

NOW = 1_790_000_000.0


def _delta(state: MindState, **kw) -> str:
    """A turn's message right after a page opened: only what ``kw`` adds."""
    seen = Seen()
    render_epoch_start(state, seen, "internal", NOW)
    return render_turn_delta(state, seen, "internal", [], NOW + 1, **kw)


def test_memory_hits_rendered_as_bullets():
    state = MindState()
    hits = [
        {"content": "user likes coffee", "source": "shared/2026-05-12.md"},
        {"content": "user lives in Taipei", "source": "shared/2026-05-13.md"},
    ]
    out = _delta(state, memory_hits=hits)
    assert "- user likes coffee" in out
    assert "- user lives in Taipei" in out


def test_open_loops_rendered():
    state = MindState()
    state.open_loops.append(OpenLoop(id="t1", desc="check tmp", opened_at=NOW - 10))
    out = render_epoch_start(state, Seen(), "internal", NOW)
    assert "t1" in out and "check tmp" in out


def test_open_loops_empty_renders_none():
    from dollos.mind.mind_prompt import _render_open_loops

    assert _render_open_loops([], time.time()) == "(none)"


def test_open_loops_stale_gets_marker():
    """P7 (spec §13.2): a loop older than OPEN_LOOP_STALE_S gets a visible
    STALE marker nudging the model to act on it or CloseLoop it."""
    from dollos.mind.mind_prompt import OPEN_LOOP_STALE_S, _render_open_loops

    now = 1_000_000.0
    loops = [OpenLoop(id="t1", desc="check tmp", opened_at=now - OPEN_LOOP_STALE_S - 1)]
    out = _render_open_loops(loops, now)
    assert "STALE" in out
    assert "CloseLoop" in out


def test_open_loops_fresh_no_marker():
    """A loop well under the threshold renders exactly as before, unmarked."""
    from dollos.mind.mind_prompt import OPEN_LOOP_STALE_S, _render_open_loops

    now = 1_000_000.0
    loops = [OpenLoop(id="t1", desc="check tmp", opened_at=now - (OPEN_LOOP_STALE_S - 60))]
    out = _render_open_loops(loops, now)
    assert "STALE" not in out
    assert "t1" in out and "check tmp" in out


def test_open_loops_exact_boundary_no_marker():
    """Strict `>` boundary: exactly at OPEN_LOOP_STALE_S is NOT stale yet."""
    from dollos.mind.mind_prompt import OPEN_LOOP_STALE_S, _render_open_loops

    now = 1_000_000.0
    loops = [OpenLoop(id="t1", desc="check tmp", opened_at=now - OPEN_LOOP_STALE_S)]
    out = _render_open_loops(loops, now)
    assert "STALE" not in out


def test_recent_perceptions_newest_last():
    state = MindState()
    state.recent_perceptions.append(Perception(kind="UserSpoke", t=100.0, data={"text": "first"}))
    state.recent_perceptions.append(Perception(kind="UserSpoke", t=200.0, data={"text": "second"}))
    out = render_epoch_start(state, Seen(), "internal", NOW)
    assert out.index("first") < out.index("second")  # newest last


def test_recent_outputs_block():
    state = MindState()
    state.recent_outputs.append(OutputRecord(t=NOW - 3, kind="Speech", summary="spoke: hi"))
    out = render_epoch_start(state, Seen(), "internal", NOW)
    assert "[Recent outputs]" in out
    assert "spoke: hi" in out


def test_recent_outputs_warns_on_recent_speech():
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    state.recent_outputs.append(OutputRecord(
        kind="Speech",
        t=time.time() - 10,
        summary="spoke: 主人晚安",
    ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" in out
    assert "spoke" in out
    # Model API plan Task 6: no wire-format talk — silence is just "say nothing".
    assert "stay silent (say nothing)" in out
    assert "tool_call" not in out


def test_recent_outputs_no_warn_old_speech():
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    state.recent_outputs.append(OutputRecord(
        kind="Speech",
        t=time.time() - 60,
        summary="spoke: 早安",
    ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" not in out


def test_recent_outputs_ignores_non_speech_recent():
    """Recent NoteMemory (e.g.) should not trigger spam warning."""
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    state.recent_outputs.append(OutputRecord(
        kind="NoteMemory",
        t=time.time() - 5,
        summary="noted: x",
    ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" not in out


def test_recent_outputs_warns_on_repeat_streak():
    """P6 (spec §13.1): a >= threshold same-action streak (non-Speech) warns,
    independently of the Speech time-window check above."""
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    for _ in range(3):
        state.recent_outputs.append(OutputRecord(
            kind="SetFocus",
            t=time.time(),
            summary="focus → same thing",
        ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" in out
    assert "SetFocus" in out
    assert "same thing" in out


def test_recent_outputs_no_repeat_warn_below_threshold():
    """Below-threshold repeats (2x) must not warn."""
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    for _ in range(2):
        state.recent_outputs.append(OutputRecord(
            kind="SetFocus",
            t=time.time(),
            summary="focus → same thing",
        ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" not in out


def test_recent_outputs_repeat_streak_excludes_speech():
    """A same-content Speech streak must NOT trigger the count-based warning
    (Speech already has its own separate time-window warning)."""
    from dollos.mind.mind_prompt import _render_outputs_header

    state = MindState()
    for _ in range(5):
        state.recent_outputs.append(OutputRecord(
            kind="Speech",
            t=time.time() - 60,  # outside the 30s Speech warning window
            summary="spoke: 早安",
        ))
    out = _render_outputs_header(list(state.recent_outputs), time.time())
    assert "WARNING" not in out


def test_repeat_loop_detected_perception_rendered():
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(
        kind="RepeatLoopDetected",
        t=time.time(),
        data={"tool": "SetFocus", "summary": "focus → same thing", "count": 3},
    )
    body = _percep_body(p)
    assert "3" in body
    assert "SetFocus" in body
    assert "focus → same thing" in body


def test_persona_drift_detected_perception_rendered():
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(
        kind="PersonaDriftDetected",
        t=time.time(),
        data={
            "violations": ["banned substring found: 'a~'"],
            "snippet": "哈囉a~最近好嗎",
        },
    )
    body = _percep_body(p)
    assert "banned substring found: 'a~'" in body
    assert "哈囉a~最近好嗎" in body
    assert "人設約束" in body


def test_bridge_down_perception_rendered():
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(
        kind="BridgeDown",
        t=time.time(),
        data={"service": "discord-bridge", "rc": 1},
    )
    body = _percep_body(p)
    assert "discord-bridge" in body
    assert "rc=1" in body
    assert "{" not in body  # must be prose, not a raw dict dump (fallback str(d))


def test_diary_moment_renders_nonempty_narrative():
    from dollos.mind.mind_prompt import _percep_body

    body = _percep_body(Perception(kind="DiaryMoment", t=0.0, data={}))
    assert body.strip()  # not blank
    assert "日記" in body


def test_pulse_moment_renders_full_detail_not_truncated_dict():
    from dollos.mind.mind_prompt import _percep_body

    long_title = "報告" * 40  # long window title pushes detail well past 120 chars
    detail = f"盯著「{long_title}」連續 118 分鐘沒換"
    p = Perception(
        kind="PulseMoment",
        t=time.time(),
        data={"concern": "window_stuck", "detail": detail},
    )
    body = _percep_body(p)
    assert body == detail  # self-contained detail rendered verbatim
    assert "連續 118 分鐘沒換" in body  # actionable tail must survive
    assert "{" not in body and "}" not in body  # must be prose, not a raw dict dump


def test_interrupted_perception_rendered():
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Interrupted", t=time.time(), data={"by": "user_text_input"})
    body = _percep_body(p)
    assert "cut short" in body.lower() or "interrupt" in body.lower()
    assert "user" in body.lower()


def test_interrupted_perception_fallback_by():
    """If `by` is missing, default to 'user'."""
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Interrupted", t=time.time(), data={})
    body = _percep_body(p)
    assert "user" in body.lower()


def test_awoke_recovered_renders_distinctly():
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Awoke", t=time.time(), data={"reason": "recovered"})
    body = _percep_body(p)
    assert "recover" in body.lower() or "crash" in body.lower()


def test_awoke_cold_start_unchanged():
    """Existing cold_start rendering preserved."""
    import time
    from dollos.mind.mind_state import Perception
    from dollos.mind.mind_prompt import _percep_body

    p = Perception(kind="Awoke", t=time.time(), data={"reason": "cold_start"})
    body = _percep_body(p)
    assert "cold_start" in body


# --- P6.3 (Task 8): safe mode on a page snapshot ---


def test_safe_mode_shown_on_page_snapshot():
    s = MindState()
    s.safe_mode = True
    s.safe_mode_reason = "3 consecutive tool failures"
    out = render_epoch_start(s, Seen(), "internal", NOW)
    assert "[Safe mode]" in out
    assert "3 consecutive tool failures" in out


def test_safe_mode_omitted_from_page_snapshot_when_off():
    out = render_epoch_start(MindState(), Seen(), "internal", NOW)
    assert "[Safe mode]" not in out


# --- Memory-writing guideline (primary language + understand-then-write) ---


def test_memory_guideline_names_the_language():
    """The [Memory guideline] (now in the system message, see
    MindLoop._system_for_turn) interpolates the configured primary_language."""
    out = render_memory_guideline("繁體中文")
    assert out.startswith("[Memory guideline]")
    assert "繁體中文" in out


def test_memory_guideline_language_is_parameterized():
    out = render_memory_guideline("English")
    assert "English" in out
    # The default must NOT leak through when a different language is configured.
    assert "繁體中文" not in out


def test_memory_guideline_says_understand_then_write_own_words():
    """The guideline must instruct understand-first, own-words, no verbatim copy."""
    lowered = render_memory_guideline("繁體中文").lower()
    assert "own words" in lowered
    assert "verbatim" in lowered


def test_tool_outcomes_block_only_when_passed():
    s = MindState()
    assert "[Tool outcomes" not in _delta(s)
    out = _delta(
        s, tool_outcomes_block="[Tool outcomes since last reflection]\n- Shell: 3 ok, 1 fail"
    )
    assert "[Tool outcomes" in out and "Shell" in out


# --- Task 7: [Tool habits] block gating ---


def test_tool_habits_block_gated():
    assert "[Tool habits]" not in _delta(MindState())
    out = _delta(
        MindState(), tool_habits_hits=[{"content": "## h\n\n[situation] grep\nuse Grep\n"}]
    )
    assert "[Tool habits]" in out and "use Grep" in out


# ---------------------------------------------------------------------------
# Whole-branch review C3: on external_public turns, scope [Recent
# perceptions] and suppress [Recent outputs] entirely — both are GLOBAL
# rolling buffers appended for EVERY turn regardless of origin, so unscoped
# they render prior owner UserSpoke/owner-DM text + Doll's prior private
# replies straight into a stranger's prompt.
# ---------------------------------------------------------------------------


def _seed_mixed_state() -> MindState:
    """One owner UserSpoke (private, must be scoped out on external_public)
    + one stranger ChannelMessage in channel c1 (public-safe, must survive
    scoping) + one prior Speech output (must be suppressed entirely on
    external_public)."""
    state = MindState()
    state.recent_perceptions.append(Perception(
        kind="UserSpoke", t=time.time(),
        data={"text": "owner-secret-marker-999"},
    ))
    state.recent_perceptions.append(Perception(
        kind="ChannelMessage", t=time.time(),
        data={
            "content": "stranger-public-marker-123", "channel": "general",
            "channel_id": "c1", "author": "rando", "author_is_owner": False,
            "is_dm": False,
        },
    ))
    state.recent_outputs.append(OutputRecord(
        t=time.time(), kind="Speech", summary="spoke: owner-private-reply-marker",
    ))
    return state


def test_external_public_scopes_recent_perceptions_and_suppresses_outputs():
    state = _seed_mixed_state()
    out = render_epoch_start(state, Seen(), "external_public", NOW, channel_id="c1")
    assert "owner-secret-marker-999" not in out
    assert "stranger-public-marker-123" in out
    assert "[Recent outputs]" not in out
    assert "owner-private-reply-marker" not in out


def test_internal_turn_control_shows_owner_marker_and_outputs():
    """Control: the private conversation recaps its own (origin-less)
    perceptions and renders [Recent outputs]."""
    state = _seed_mixed_state()
    out = render_epoch_start(state, Seen(), "internal", NOW)
    assert "owner-secret-marker-999" in out
    assert "[Recent outputs]" in out
    assert "owner-private-reply-marker" in out


def test_external_dm_turn_control_shows_outputs():
    """Control: external_dm (owner's own DM) keeps [Recent outputs]."""
    state = _seed_mixed_state()
    out = render_epoch_start(state, Seen(), "external_dm", NOW, channel_id="dm1")
    assert "[Recent outputs]" in out
    assert "owner-private-reply-marker" in out


def test_external_public_teeth_owner_marker_would_leak_unfiltered():
    """Teeth: prove the owner marker genuinely lives in recent_perceptions
    (i.e. this isn't a vacuous pass because the marker never renders at all)
    — the unfiltered renderer DOES include it, so the scoped snapshot
    excluding it is a real, non-trivial effect of the scoping."""
    from dollos.mind.mind_prompt import _render_perceptions
    state = _seed_mixed_state()
    unfiltered = _render_perceptions(state.recent_perceptions, time.time())
    assert "owner-secret-marker-999" in unfiltered


# --- Task 2: [Body signal] framing block ---


def test_body_signal_critical_wording():
    from dollos.mind.mind_prompt import render_body_signal
    out = render_body_signal([("critical", "電量掉到 12% 而且在放電")])
    assert out is not None
    assert out.startswith("[Body signal]")
    assert "電量掉到 12% 而且在放電" in out
    assert "主人多半會想知道" in out          # critical lean-to-surface phrasing
    assert "不緊急" not in out


def test_body_signal_advisory_wording():
    from dollos.mind.mind_prompt import render_body_signal
    out = render_body_signal([("advisory", "盯著「editor」連續 118 分鐘沒換")])
    assert out is not None
    assert "不緊急" in out                    # advisory lean-to-restraint phrasing
    assert "主人多半會想知道" not in out
    assert "連續 118 分鐘沒換" in out


def test_body_signal_mixed_takes_critical():
    from dollos.mind.mind_prompt import render_body_signal
    out = render_body_signal([("advisory", "卡視窗"), ("critical", "電量低")])
    assert "主人多半會想知道" in out          # any critical → critical framing
    assert "卡視窗" in out and "電量低" in out


def test_body_signal_empty_returns_none():
    from dollos.mind.mind_prompt import render_body_signal
    assert render_body_signal([]) is None
