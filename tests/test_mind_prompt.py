"""Tests for render_mind — the MindLoop prompt renderer."""
import time
from collections import deque

import pytest

from dollos.mind.mind_state import (
    MindState, Mood, ActiveTask, OpenLoop, PendingEvent,
    Perception, OutputRecord,
)
from dollos.mind.mind_prompt import render_mind


def test_renders_all_blocks_in_order():
    state = MindState(focus="testing", mood=Mood(emotion="calm", reason="just woke up"))
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYSTEM PROMPT HERE")
    # Order check via index-of
    expected_order = [
        "SYSTEM PROMPT HERE",
        "[Memory context]",
        "[Mind state]",
        "[Active tasks]",
        "[Open loops]",
        "[Pending]",
        "[Scratchpad]",
        "[Recent perceptions]",
        "[Recent outputs]",
        "[Decision time]",
    ]
    last_idx = -1
    for marker in expected_order:
        idx = prompt.index(marker)
        assert idx > last_idx, f"{marker} out of order"
        last_idx = idx


def test_empty_state_renders_none_placeholders():
    state = MindState()
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    assert "(no relevant memories)" in prompt
    assert "[Active tasks]" in prompt and "(none)" in prompt
    assert "(empty)" in prompt  # scratchpad


def test_memory_hits_rendered_as_bullets():
    state = MindState()
    hits = [
        {"content": "user likes coffee", "source": "shared/2026-05-12.md"},
        {"content": "user lives in Taipei", "source": "shared/2026-05-13.md"},
    ]
    prompt = render_mind(state, memsearch_hits=hits, system_prompt="SYS")
    assert "- user likes coffee" in prompt
    assert "- user lives in Taipei" in prompt


def test_active_tasks_show_elapsed():
    state = MindState()
    state.active_tasks.append(ActiveTask(
        task_id="shell-1", kind="shell", summary="ls /tmp",
        started_at=time.time() - 5.0,
    ))
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    assert "shell-1" in prompt
    assert "ls /tmp" in prompt
    # elapsed shown
    assert "5" in prompt  # ~5s elapsed


def test_open_loops_rendered():
    state = MindState()
    state.open_loops.append(OpenLoop(id="t1", desc="check tmp", opened_at=time.time() - 10))
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    assert "t1" in prompt and "check tmp" in prompt


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
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    idx_first = prompt.index("first")
    idx_second = prompt.index("second")
    assert idx_first < idx_second  # newest last


def test_recent_outputs_block():
    state = MindState()
    state.recent_outputs.append(OutputRecord(t=time.time() - 3, kind="Speech", summary="spoke: hi"))
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    assert "spoke: hi" in prompt
    assert "don't repeat yourself" in prompt.lower()


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


def test_decision_time_marker_is_last():
    state = MindState()
    prompt = render_mind(state, memsearch_hits=[], system_prompt="SYS")
    assert prompt.rstrip().endswith("0..N actions.") or "JSON array" in prompt[prompt.rfind("[Decision time]"):]


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


def test_recent_self_review_block_rendered():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.recent_reviews.append("did not check the file first")
    out = render_mind(s, [], "SYS")
    assert "[Recent self-review]" in out
    assert "did not check the file first" in out


def test_recent_self_review_block_omitted_when_empty():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    out = render_mind(s, [], "SYS")
    assert "[Recent self-review]" not in out


def test_recent_self_review_renders_oldest_to_newest():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.recent_reviews.append("first lesson")
    s.recent_reviews.append("second lesson")
    out = render_mind(s, [], "SYS")
    assert out.index("first lesson") < out.index("second lesson")


# --- P6.3 (Task 8): [Safe mode] banner ---


def test_safe_mode_banner_rendered():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.safe_mode = True
    s.safe_mode_reason = "3 consecutive tool failures"
    out = render_mind(s, [], "SYS")
    assert "[Safe mode]" in out
    assert "3 consecutive tool failures" in out


def test_safe_mode_banner_omitted_when_off():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    out = render_mind(s, [], "SYS")
    assert "[Safe mode]" not in out


# --- Memory-writing guideline (primary language + understand-then-write) ---


def test_memory_guideline_present_with_default_language():
    """Every turn carries a [Memory guideline] block; default language is 繁體中文."""
    state = MindState()
    out = render_mind(state, [], "SYS")
    assert "[Memory guideline]" in out
    assert "繁體中文" in out


def test_memory_guideline_language_is_parameterized():
    """The guideline interpolates the configured primary_language."""
    state = MindState()
    out = render_mind(state, [], "SYS", primary_language="English")
    assert "[Memory guideline]" in out
    assert "English" in out
    # The default must NOT leak through when a different language is configured.
    assert "繁體中文" not in out


def test_memory_guideline_says_understand_then_write_own_words():
    """The guideline must instruct understand-first, own-words, no verbatim copy."""
    state = MindState()
    out = render_mind(state, [], "SYS")
    guideline = out[out.index("[Memory guideline]"):out.index("[Memory context]")]
    lowered = guideline.lower()
    assert "own words" in lowered
    assert "verbatim" in lowered


def test_render_mind_includes_tool_notes_when_recent_failures():
    import time
    from collections import deque
    from dollos.mind.mind_state import MindState, ToolFailure
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    s.recent_tool_failures = deque(
        [ToolFailure(t=time.time(), tool="Shell", detail="timeout after 60s")], maxlen=10
    )
    out = render_mind(s, [], "sys")
    assert "[Tool notes]" in out and "Shell" in out and "timeout" in out


def test_render_mind_omits_tool_notes_when_no_failures():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    out = render_mind(MindState(), [], "sys")
    assert "[Tool notes]" not in out


def test_tool_outcomes_block_only_when_passed():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    s = MindState()
    assert "[Tool outcomes" not in render_mind(s, [], "sys")
    out = render_mind(s, [], "sys", tool_outcomes_block="[Tool outcomes since last reflection]\n- Shell: 3 ok, 1 fail")
    assert "[Tool outcomes" in out and "Shell" in out


# --- Task 7: [Tool habits] block gating ---


def test_tool_habits_block_gated():
    from dollos.mind.mind_state import MindState
    from dollos.mind.mind_prompt import render_mind
    assert "[Tool habits]" not in render_mind(MindState(), [], "sys")
    out = render_mind(MindState(), [], "sys",
                      tool_habits_hits=[{"content": "## h\n\n[situation] grep\nuse Grep\n"}])
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
    + one stranger ChannelMessage (public-safe, must survive scoping) +
    one prior Speech output (must be suppressed entirely on external_public)."""
    state = MindState()
    state.recent_perceptions.append(Perception(
        kind="UserSpoke", t=time.time(),
        data={"text": "owner-secret-marker-999"},
    ))
    state.recent_perceptions.append(Perception(
        kind="ChannelMessage", t=time.time(),
        data={
            "content": "stranger-public-marker-123", "channel": "general",
            "author": "rando", "author_is_owner": False, "is_dm": False,
        },
    ))
    state.recent_outputs.append(OutputRecord(
        t=time.time(), kind="Speech", summary="spoke: owner-private-reply-marker",
    ))
    return state


def test_external_public_scopes_recent_perceptions_and_suppresses_outputs():
    state = _seed_mixed_state()
    out = render_mind(state, [], "SYS", origin_tier="external_public")
    assert "owner-secret-marker-999" not in out
    assert "stranger-public-marker-123" in out
    assert "[Recent outputs]" not in out
    assert "owner-private-reply-marker" not in out


def test_internal_turn_control_shows_owner_marker_and_outputs():
    """Control: internal (default origin_tier) renders BOTH blocks in full,
    unchanged by this fix."""
    state = _seed_mixed_state()
    out = render_mind(state, [], "SYS")  # origin_tier defaults to "internal"
    assert "owner-secret-marker-999" in out
    assert "stranger-public-marker-123" in out
    assert "[Recent outputs]" in out
    assert "owner-private-reply-marker" in out


def test_external_dm_turn_control_shows_owner_marker_and_outputs():
    """Control: external_dm (owner's own DM) is NOT scoped — owner sees her
    own full working memory, mirroring the retrieval-scope control tests."""
    state = _seed_mixed_state()
    out = render_mind(state, [], "SYS", origin_tier="external_dm")
    assert "owner-secret-marker-999" in out
    assert "[Recent outputs]" in out
    assert "owner-private-reply-marker" in out


def test_external_public_teeth_owner_marker_would_leak_unfiltered():
    """Teeth: prove the owner marker genuinely lives in recent_perceptions
    (i.e. this isn't a vacuous pass because the marker never renders at all)
    — the unfiltered renderer DOES include it, so the scoped render_mind
    excluding it is a real, non-trivial effect of the C3 filter."""
    from dollos.mind.mind_prompt import _render_perceptions
    state = _seed_mixed_state()
    unfiltered = _render_perceptions(state.recent_perceptions, time.time())
    assert "owner-secret-marker-999" in unfiltered
