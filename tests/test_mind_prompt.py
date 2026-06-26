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
