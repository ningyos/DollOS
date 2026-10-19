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
    assert "persona constraints" in body


def test_persona_drift_describes_without_commanding():
    """Plan 2026-10-19 Task 5: the perception says what happened — which
    constraints, what she said — in English, with no "watch out next time"
    command; several violations are joined with ", "."""
    from dollos.mind.mind_prompt import _percep_body

    body = _percep_body(Perception(
        kind="PersonaDriftDetected",
        t=time.time(),
        data={"violations": ["v-one", "v-two"], "snippet": "S"},
    ))
    assert body == 'what you just said broke your persona constraints (v-one, v-two): "S"'


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
    assert "lost one of your online channels" in body


def test_mcp_down_perception_rendered():
    from dollos.mind.mind_prompt import _percep_body

    body = _percep_body(Perception(kind="McpDown", t=time.time(),
                                   data={"service": "mcp-server", "rc": 2}))
    assert "lost one of your online channels" in body
    assert "mcp-server (the port external AIs connect through)" in body
    assert "rc=2" in body


def test_diary_moment_renders_nonempty_narrative():
    from dollos.mind.mind_prompt import _percep_body

    body = _percep_body(Perception(kind="DiaryMoment", t=0.0, data={}))
    assert body.strip()  # not blank
    assert "diary" in body
    assert "[Today's log]" in body and "WriteDiary" in body


def test_pulse_moment_renders_full_detail_not_truncated_dict():
    from dollos.mind.mind_prompt import _percep_body

    long_title = "報告" * 40  # long window title pushes detail well past 120 chars
    detail = f'staring at "{long_title}" for 118 minutes without switching'
    p = Perception(
        kind="PulseMoment",
        t=time.time(),
        data={"concern": "window_stuck", "detail": detail},
    )
    body = _percep_body(p)
    assert body == detail  # self-contained detail rendered verbatim
    assert "for 118 minutes without switching" in body  # actionable tail must survive
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
    out = render_memory_guideline("English")
    assert out.startswith("[Memory guideline]")
    assert "is in English" in out


def test_memory_guideline_language_is_parameterized():
    out = render_memory_guideline("Japanese")
    assert "is in Japanese" in out
    # The default must NOT leak through when a different language is configured.
    assert "English" not in out


def test_memory_guideline_covers_everything_she_writes():
    """All-English plan Task 2: the guideline names everything she writes
    down, not only notes and diary — self-profile, agenda and schedule came
    out English while notes and diary stayed Chinese because they were not
    covered; "who I am now" is hers too (she rewrites it via SelfRevision)."""
    out = render_memory_guideline("English")
    for kind in ("notes", "diary", "who I am now", "self-profile", "agenda", "schedule"):
        assert kind in out
    assert "Your notes and diary are written in" not in out


def test_memory_guideline_says_understand_then_write_own_words():
    """The guideline describes understand-first, own-words, no verbatim copy."""
    lowered = render_memory_guideline("English").lower()
    assert "own words" in lowered
    assert "verbatim" in lowered


def test_memory_guideline_describes_rather_than_commands():
    """Self-First (English-speech plan Task 3): the guideline describes how
    everything she writes down is written; the old command phrasing stays
    out."""
    out = render_memory_guideline("English")
    assert out == (
        "[Memory guideline] Everything you write down — notes, diary, who I "
        "am now, self-profile, agenda, schedule — is in English (proper nouns "
        "and technical terms may stay in their original language), in your "
        "own words — you understand something first and write what it means "
        "to you; you don't copy source text verbatim."
    )
    for command in ("write it in", "Record your", "never copy"):
        assert command not in out


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
    out = render_body_signal([("critical", "battery down to 12% and discharging")])
    assert out is not None
    assert out.startswith("[Body signal]")
    assert "battery down to 12% and discharging" in out
    assert "your owner would most likely want to know" in out  # critical lean-to-surface
    assert "Not urgent" not in out


def test_body_signal_advisory_wording():
    from dollos.mind.mind_prompt import render_body_signal
    out = render_body_signal([("advisory", 'staring at "editor" for 118 minutes without switching')])
    assert out is not None
    assert "Not urgent" in out                # advisory lean-to-restraint phrasing
    assert "would most likely want to know" not in out
    assert "for 118 minutes without switching" in out


def test_body_signal_mixed_takes_critical():
    from dollos.mind.mind_prompt import render_body_signal
    out = render_body_signal([("advisory", "window stuck"), ("critical", "battery low")])
    assert "would most likely want to know" in out  # any critical → critical framing
    assert "window stuck; battery low" in out       # details joined with "; "


def test_body_signal_empty_returns_none():
    from dollos.mind.mind_prompt import render_body_signal
    assert render_body_signal([]) is None
