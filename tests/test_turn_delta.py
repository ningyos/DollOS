"""Snapshot + per-turn delta rendering (plan 2026-10-18 Task 8).

``render_epoch_start`` writes the one user message that opens a conversation
page; ``render_turn_delta`` writes each turn's new user message, carrying only
what changed since the conversation last saw it (tracked in its ``Seen``).
Also covers the shared fixes that land with them: the four perception-line
bugs, the [Memory context] query / transcripts exclusion / 3-hit cap, the
Recall snippet and the tool-message cap.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from dollos.cascade.tool_loop import ToolResult, run_tool_calls
from dollos.llm.model import SpeechDelta, ToolCall
from dollos.memory import FtsMemory
from dollos.mind.mind_prompt import (
    MEMORY_HIT_MAX_CHARS,
    MEMORY_HITS_PER_TURN,
    Seen,
    _percep_body,
    hits_in_context,
    render_epoch_start,
    render_turn_delta,
)
from dollos.mind.mind_state import MindState, Mood, OpenLoop, OutputRecord, Perception
from dollos.tools import Recall
from tests._dispatcher_helpers import _FakeMemSearch, _make_mind_ctx
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop

NOW = 1_790_000_000.0  # fixed clock: every rendered time is deterministic


def _clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _hit(n: int, content: str | None = None, source: str | None = None) -> dict:
    return {
        "content": content if content is not None else f"memory-fact-{n}",
        "source": source or f"/m/shared/2026-10-{n:02d}.md",
        "chunk_hash": f"h{n}",
    }


def _state() -> MindState:
    s = MindState(focus="sorting photos", mood=Mood(emotion="開心", reason="the sun came out"))
    s.scratchpad = "remember the ferry times"
    s.open_loops.append(OpenLoop(id="L1", desc="call the dentist", opened_at=NOW - 86_400))
    s.open_loops.append(OpenLoop(
        id="G1", desc="learn the harbour birds", opened_at=NOW - 7_200,
        self_directed=True, trigger="saw gulls",
    ))
    return s


# ---------------------------------------------------------------------------
# Snapshot blocks: resent only when their text changed
# ---------------------------------------------------------------------------


_HEADERS = ("[Self profile]", "[Mind state]", "[Your agenda]", "[Open loops]", "[Scratchpad]")
_PROFILE = "- s1: likes tea"


def test_snapshot_block_resent_only_when_its_text_changed():
    state, seen = _state(), Seen()
    start = render_epoch_start(state, seen, "internal", NOW, self_profile_text=_PROFILE)
    for header in _HEADERS:
        assert header in start

    quiet = render_turn_delta(state, seen, "internal", [], NOW + 60, self_profile_text=_PROFILE)
    for header in _HEADERS:
        assert header not in quiet

    # Changed by anyone (another conversation, a background job): resent whole, once.
    state.mood = Mood(emotion="煩", reason="rain again")
    state.scratchpad = "ferry at 9"
    changed = render_turn_delta(state, seen, "internal", [], NOW + 120, self_profile_text=_PROFILE)
    assert "[Mind state]" in changed and "煩 (rain again)" in changed
    assert "focus: sorting photos" in changed  # the whole block, not a diff
    assert "[Scratchpad]\nferry at 9" in changed
    assert "[Your agenda]" not in changed and "[Self profile]" not in changed

    again = render_turn_delta(state, seen, "internal", [], NOW + 180, self_profile_text=_PROFILE)
    assert "[Mind state]" not in again and "[Scratchpad]" not in again


def test_each_conversation_tracks_its_own_seen():
    state, a, b = _state(), Seen(), Seen()
    render_epoch_start(state, a, "internal", NOW)
    render_epoch_start(state, b, "external_dm", NOW)
    state.focus = "packing"
    assert "focus: packing" in render_turn_delta(state, a, "internal", [], NOW + 1)
    assert "focus: packing" in render_turn_delta(state, b, "external_dm", [], NOW + 2)


def test_self_profile_appearing_later_is_sent():
    state, seen = _state(), Seen()
    start = render_epoch_start(state, seen, "internal", NOW, self_profile_text=None)
    assert "[Self profile]" not in start
    assert "[Self profile]" not in render_turn_delta(state, seen, "internal", [], NOW + 1)
    delta = render_turn_delta(state, seen, "internal", [], NOW + 2, self_profile_text="- s1: new")
    assert "[Self profile]" in delta and "- s1: new" in delta


# ---------------------------------------------------------------------------
# Memory hits: once per page, at most 3 new per turn, each <= 300 chars
# ---------------------------------------------------------------------------


def test_hit_appears_once_per_page():
    state, seen = _state(), Seen()
    render_epoch_start(state, seen, "internal", NOW)
    first = render_turn_delta(state, seen, "internal", [], NOW + 1, memory_hits=[_hit(1)])
    assert "[Memory context]" in first and "memory-fact-1" in first
    second = render_turn_delta(state, seen, "internal", [], NOW + 2, memory_hits=[_hit(1)])
    assert "memory-fact-1" not in second and "[Memory context]" not in second

    # A new page starts with nothing seen.
    fresh = Seen()
    render_epoch_start(state, fresh, "internal", NOW + 3)
    assert "memory-fact-1" in render_turn_delta(
        state, fresh, "internal", [], NOW + 4, memory_hits=[_hit(1)]
    )


def test_associative_and_habit_hits_also_appear_once_per_page():
    state, seen = _state(), Seen()
    render_epoch_start(state, seen, "internal", NOW)
    assoc = [{**_hit(7, content="assoc-fact"), "_axis": "tod", "_axis_value": "evening"}]
    habit = [{"content": "## h\n\n[situation] grep logs\nuse GrepToolOutput\n", "chunk_hash": "hb"}]
    first = render_turn_delta(
        state, seen, "internal", [], NOW + 1, associative_hits=assoc, tool_habits_hits=habit
    )
    assert "assoc-fact" in first and "use GrepToolOutput" in first
    second = render_turn_delta(
        state, seen, "internal", [], NOW + 2, associative_hits=assoc, tool_habits_hits=habit
    )
    assert "assoc-fact" not in second and "GrepToolOutput" not in second


def test_at_most_three_new_hits_per_turn_each_capped():
    assert MEMORY_HITS_PER_TURN == 3 and MEMORY_HIT_MAX_CHARS == 300
    state, seen = _state(), Seen()
    render_epoch_start(state, seen, "internal", NOW)
    hits = [_hit(n, content=f"fact{n} " + "x" * 1000) for n in range(1, 6)]

    first = render_turn_delta(state, seen, "internal", [], NOW + 1, memory_hits=hits)
    bullets = [ln for ln in first.splitlines() if ln.startswith("- fact")]
    assert [b.split()[1] for b in bullets] == ["fact1", "fact2", "fact3"]
    assert all(len(b) - len("- ") <= MEMORY_HIT_MAX_CHARS for b in bullets)

    # The two held back are still new on this page: the next turn gets them.
    second = render_turn_delta(state, seen, "internal", [], NOW + 2, memory_hits=hits)
    bullets = [ln for ln in second.splitlines() if ln.startswith("- fact")]
    assert [b.split()[1] for b in bullets] == ["fact4", "fact5"]


def test_hits_in_context_is_what_she_was_actually_shown():
    state, seen = _state(), Seen()
    render_epoch_start(state, seen, "internal", NOW)
    hits = [_hit(n) for n in range(1, 6)]
    render_turn_delta(state, seen, "internal", [], NOW + 1, memory_hits=hits)
    assert [h["chunk_hash"] for h in hits_in_context(hits, seen)] == ["h1", "h2", "h3"]

    # Next turn re-finds h2 (already on the page) and h5 (new): both are in context.
    later = [_hit(2), _hit(5)]
    render_turn_delta(state, seen, "internal", [], NOW + 2, memory_hits=later)
    assert [h["chunk_hash"] for h in hits_in_context(later, seen)] == ["h2", "h5"]


# ---------------------------------------------------------------------------
# Recap: this conversation's own perceptions only, not this turn's
# ---------------------------------------------------------------------------


def test_recap_has_only_this_conversations_earlier_perceptions():
    state = _state()
    old_private = Perception(kind="UserSpoke", t=NOW - 300, data={"text": "private-old"})
    other_channel = Perception(kind="ChannelMessage", t=NOW - 200, data={
        "channel_id": "c1", "content": "channel-old", "channel": "general",
        "author": "rando", "author_is_owner": False, "is_dm": False,
    })
    current = Perception(kind="UserSpoke", t=NOW - 1, data={"text": "this-turn"})
    for p in (old_private, other_channel, current):
        state.recent_perceptions.append(p)

    private = render_epoch_start(state, Seen(), "internal", NOW, channel_id="", exclude=[current])
    assert f"{_clock(NOW - 300)} UserSpoke: 'private-old'" in private
    assert "channel-old" not in private
    assert "this-turn" not in private

    channel = render_epoch_start(state, Seen(), "external_public", NOW, channel_id="c1")
    assert "channel-old" in channel
    assert "private-old" not in channel and "this-turn" not in channel


def test_recap_oldest_first_and_outputs_by_tier():
    state = _state()
    for t, text in ((NOW - 50, "first"), (NOW - 40, "second")):
        state.recent_perceptions.append(Perception(kind="UserSpoke", t=t, data={"text": text}))
    state.recent_outputs.append(OutputRecord(t=NOW - 45, kind="Speech", summary="spoke: hello"))
    out = render_epoch_start(state, Seen(), "internal", NOW)
    assert out.index("first") < out.index("second")
    assert f"{_clock(NOW - 45)} spoke: hello" in out
    assert "spoke: hello" in render_epoch_start(state, Seen(), "external_dm", NOW, channel_id="dm1")


# ---------------------------------------------------------------------------
# No relative time, no iter, no retired blocks; every perception line has HH:MM
# ---------------------------------------------------------------------------


def test_no_relative_time_iter_or_retired_blocks():
    from collections import deque

    from dollos.mind.mind_state import ToolFailure

    state = _state()
    state.iter_count = 42
    state.last_user_at = NOW - 40
    state.safe_mode, state.safe_mode_reason = True, "3 failures"
    state.recent_outputs.append(OutputRecord(t=NOW - 5, kind="Speech", summary="spoke: hi"))
    for _ in range(3):
        state.recent_outputs.append(OutputRecord(t=NOW - 4, kind="SetFocus", summary="focus → x"))
    state.recent_tool_failures = deque(
        [ToolFailure(t=NOW - 3, tool="Shell", detail="boom")], maxlen=10
    )
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=NOW - 40, data={"text": "earlier"})
    )
    batch = [
        Perception(kind="UserSpoke", t=NOW - 2, data={"text": "now then"}),
        Perception(kind="ScheduledMoment", t=NOW - 1,
                   data={"entry_time": "09:00", "intent": "stretch"}),
    ]
    seen = Seen()
    start = render_epoch_start(state, seen, "internal", NOW)
    state.mood = Mood(emotion="累", reason="long day")
    delta = render_turn_delta(state, seen, "internal", batch, NOW, memory_hits=[_hit(1)])

    for text in (start, delta):
        assert not re.search(r"\d+(\.\d+)?[smh] ago", text)
        assert "iter" not in text
        assert "STALE" not in text and "WARNING" not in text
        for retired in ("[Active tasks]", "[Pending]", "[Tool notes]", "[Decision time]",
                        "[Memory guideline]"):
            assert retired not in text
    assert delta.startswith(f"[{time.strftime('%H:%M %a %m-%d', time.localtime(NOW))}]")
    lines = delta.splitlines()
    assert f"{_clock(NOW - 2)} UserSpoke: 'now then'" in lines
    assert f"{_clock(NOW - 1)} ScheduledMoment: stretch" in lines
    # Loops show absolute dates.
    opened = time.strftime("%m-%d %H:%M", time.localtime(NOW - 86_400))
    assert f"L1: call the dentist (opened {opened})" in start


# ---------------------------------------------------------------------------
# external_public: only the mind-loop §10 blocks
# ---------------------------------------------------------------------------


def test_external_public_renders_only_allowed_blocks():
    state = _state()
    state.recent_outputs.append(
        OutputRecord(t=NOW - 5, kind="Speech", summary="spoke: private-reply")
    )
    state.recent_perceptions.append(Perception(kind="ChannelMessage", t=NOW - 9, data={
        "channel_id": "pub", "content": "earlier in pub", "channel": "general",
        "author": "rando", "author_is_owner": False, "is_dm": False,
    }))
    seen = Seen()
    start = render_epoch_start(
        state, seen, "external_public", NOW, channel_id="pub",
        self_profile_text="- s1: likes tea", energy_line="精力: 普通 (0.5)",
        pulse_block="[Self pulse]\npulse-marker", cognition_block="[Cognition]\ncog-marker",
    )
    assert "[Self profile]" in start
    assert "mood: 開心" in start and "the sun came out" not in start
    assert "focus" not in start
    assert "[External situation]" in start and "earlier in pub" in start
    for absent in ("[Your agenda]", "[Open loops]", "[Scratchpad]", "[Recent outputs]",
                   "private-reply", "pulse-marker", "cog-marker"):
        assert absent not in start
    first_line = start.splitlines()[0]
    assert first_line.startswith("[New conversation page") and "Recall" not in first_line

    state.mood = Mood(emotion="煩", reason="owner-private-reason")
    state.focus = "owner-private-focus"
    state.scratchpad = "owner-private-scratch"
    delta = render_turn_delta(
        state, seen, "external_public", [], NOW + 1,
        memory_hits=[_hit(1)], associative_hits=[{**_hit(2), "_axis": "tod"}],
        tool_habits_hits=[{"content": "## h\n\n[situation] s\nlesson\n", "chunk_hash": "hb"}],
        pulse_block="[Self pulse]\npulse-marker", cognition_block="[Cognition]\ncog-marker",
        tool_outcomes_block="[Tool outcomes since last reflection]\n- Shell: 1 ok",
        evolution_block="[人格演化候選]\nevo-marker",
        body_signal_block="[Body signal]\nbody-marker",
    )
    assert "mood: 煩" in delta
    for absent in ("owner-private", "[Memory context]", "[Associative memories]", "[Tool habits]",
                   "pulse-marker", "cog-marker", "[Tool outcomes", "evo-marker", "body-marker"):
        assert absent not in delta


def test_owner_tiers_page_header_points_at_recall():
    for tier, channel in (("internal", ""), ("external_dm", "dm1")):
        first = render_epoch_start(_state(), Seen(), tier, NOW, channel_id=channel).splitlines()[0]
        assert "use Recall" in first


def test_private_events_render_on_internal_turns():
    state, seen = _state(), Seen()
    start = render_epoch_start(
        state, seen, "internal", NOW,
        pulse_block="[Self pulse]\npulse-now", cognition_block="[Cognition]\ncog-now",
    )
    assert "pulse-now" in start and "cog-now" in start
    delta = render_turn_delta(
        state, seen, "internal", [], NOW + 1,
        pulse_block="[Self pulse]\npulse-shift",
        tool_outcomes_block="[Tool outcomes since last reflection]\n- Shell: 1 ok",
        evolution_block="[人格演化候選]\nevo-marker",
        body_signal_block="[Body signal]\nbody-marker",
    )
    for marker in ("pulse-shift", "[Tool outcomes", "evo-marker", "body-marker"):
        assert marker in delta


# ---------------------------------------------------------------------------
# Safe mode is global: every conversation hears the change once
# ---------------------------------------------------------------------------


def test_safe_mode_change_reaches_every_conversation_once():
    state, a, b = _state(), Seen(), Seen()
    assert "[Safe mode]" not in render_epoch_start(state, a, "internal", NOW)
    render_epoch_start(state, b, "external_dm", NOW, channel_id="dm1")

    # Entered during a's turn: b still hears it on its next delta.
    state.safe_mode, state.safe_mode_reason = True, "3 consecutive tool failures"
    assert "3 consecutive tool failures" in render_turn_delta(state, a, "internal", [], NOW + 1)
    on_b = render_turn_delta(state, b, "external_dm", [], NOW + 2)
    assert "[Safe mode]" in on_b and "3 consecutive tool failures" in on_b
    assert "[Safe mode]" not in render_turn_delta(state, b, "external_dm", [], NOW + 3)

    state.safe_mode, state.safe_mode_reason = False, ""
    off_a = render_turn_delta(state, a, "internal", [], NOW + 4)
    off_b = render_turn_delta(state, b, "external_dm", [], NOW + 5)
    assert "[Safe mode] off" in off_a and "[Safe mode] off" in off_b
    assert "[Safe mode]" not in render_turn_delta(state, a, "internal", [], NOW + 6)


def test_page_opened_in_safe_mode_says_so():
    state = _state()
    state.safe_mode, state.safe_mode_reason = True, "3 consecutive tool failures"
    seen = Seen()
    assert "3 consecutive tool failures" in render_epoch_start(state, seen, "internal", NOW)
    assert "[Safe mode]" not in render_turn_delta(state, seen, "internal", [], NOW + 1)


# ---------------------------------------------------------------------------
# The four perception-line bugs (real producer payloads)
# ---------------------------------------------------------------------------


def test_scheduled_moment_renders_the_producers_intent():
    # kernel._schedule_runner's payload
    p = Perception(kind="ScheduledMoment", t=NOW,
                   data={"entry_time": "2026-10-19T09:00:00", "intent": "drink water"})
    assert "drink water" in _percep_body(p)


def test_tool_result_shows_its_output_id():
    # shell_runner's payload
    shell = Perception(kind="ToolResultArrived", t=NOW, data={
        "tool": "Shell", "task_id": "sh-1", "status": "ok", "summary": "exit 0: hello",
        "output_id": "out-abc123", "line_count": 42, "command": "ls", "exit_code": 0,
    })
    body = _percep_body(shell)
    assert "out-abc123" in body and "42 lines" in body
    # workflow._emit's payload
    wf = Perception(kind="ToolResultArrived", t=NOW, data={
        "tool": "Workflow", "task_id": "wf-1", "status": "ok", "summary": "done",
        "details": "preview", "details_output_id": "out-def456", "details_line_count": 9,
        "task": "workflow: 1 tasks, mode=map_reduce",
    })
    body = _percep_body(wf)
    assert "out-def456" in body and "9 lines" in body


def test_user_spoke_is_not_cut_at_200_chars():
    text = "word " * 120 + "the-end-marker"
    body = _percep_body(Perception(kind="UserSpoke", t=NOW, data={"text": text}))
    assert "the-end-marker" in body


def test_agenda_moment_is_a_sentence_not_a_dict():
    body = _percep_body(Perception(kind="AgendaMoment", t=NOW, data={}))
    assert "{" not in body
    assert "time for your own goals" in body and "nothing you say now is heard" in body


# ---------------------------------------------------------------------------
# [Memory context] auto-injection in MindLoop
# ---------------------------------------------------------------------------


def _fts(tmp_path: Path) -> FtsMemory:
    for d in ("shared", "transcripts"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return FtsMemory(
        paths=[str(tmp_path / "shared"), str(tmp_path / "transcripts")],
        db_path=tmp_path / "fts.db",
    )


@pytest.mark.asyncio
async def test_transcripts_are_not_auto_injected_but_recall_finds_them(tmp_path):
    ms = _fts(tmp_path)
    try:
        (tmp_path / "transcripts" / "2026-10-19.md").write_text(
            "- 09:00:00 主人說：ferry-unique-word at nine\n", encoding="utf-8"
        )
        (tmp_path / "shared" / "2026-10-19.md").write_text(
            "## 09:01:00\n\nferry-unique-word leaves from pier 3\n", encoding="utf-8"
        )
        await ms.index()
        ml = make_mindloop(memory_root=tmp_path)
        ml._ctx.memsearch = ms

        hits = await ml._derive_memory_hits(
            [Perception(kind="UserSpoke", t=NOW, data={"text": "ferry-unique-word"})]
        )
        sources = [h["source"] for h in hits]
        assert any("/shared/" in s for s in sources)
        assert not any("/transcripts/" in s for s in sources)

        out = await Recall(query="ferry-unique-word").run(ml._ctx)
        assert "at nine" in out
    finally:
        ms.close()


class _QueryLog(_FakeMemSearch):
    def __init__(self, hits=None):
        super().__init__(hits)
        self.queries: list[str] = []

    async def search(self, query, top_k=5, **_kw):
        self.queries.append(query)
        return self._hits


@pytest.mark.asyncio
async def test_memory_query_uses_this_turns_own_input(tmp_path):
    ms = _QueryLog()
    ml = make_mindloop(memory_root=tmp_path)
    ml._ctx.memsearch = ms
    ml._state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=NOW - 600, data={"text": "old-local-question"})
    )

    await ml._derive_memory_hits(
        [Perception(kind="UserSpoke", t=NOW, data={"text": "fresh question"})]
    )
    ml._ctx.origin_tier = "external_dm"
    await ml._derive_memory_hits([Perception(kind="ChannelMessage", t=NOW, data={
        "channel_id": "dm1", "content": "dm question", "author_is_owner": True, "is_dm": True,
    })])
    assert ms.queries == ["fresh question", "dm question"]


@pytest.mark.asyncio
async def test_turn_memory_sources_exclude_hits_held_back_by_the_cap(tmp_path):
    hits = [_hit(n, content=f"fact{n} " + "y" * 600) for n in range(1, 6)]
    model = FakeModel([[SpeechDelta("ok")]])
    ml = make_mindloop(memory_root=tmp_path, llm=model)
    ml._ctx.memsearch = _QueryLog(hits)

    await ml._run_one_turn([Perception(kind="UserSpoke", t=NOW, data={"text": "facts?"})])

    assert ml._ctx.turn_memory_sources == [h["source"] for h in hits[:3]]
    user = model.calls[0]["messages"][1]["content"]
    bullets = [ln for ln in user.splitlines() if ln.startswith("- fact")]
    assert [b.split()[1] for b in bullets] == ["fact1", "fact2", "fact3"]
    assert all(len(b) - len("- ") <= MEMORY_HIT_MAX_CHARS for b in bullets)


# ---------------------------------------------------------------------------
# Tool results stay in the conversation, so they are kept small
# ---------------------------------------------------------------------------


class _OneHit:
    def __init__(self, content: str):
        self._content = content

    async def search(self, query, top_k=5, **_kw):
        return [{"content": self._content, "source": "/m/transcripts/2026-10-19.md"}]


@pytest.mark.asyncio
async def test_recall_hit_is_cut_around_the_query_term(tmp_path):
    content = (
        "morning chatter. " * 100 + "the dentist is on friday at three. "
        + "evening chatter. " * 100
    )
    ctx = _make_mind_ctx(tmp_path, memsearch=_OneHit(content))
    out = await Recall(query="dentist").run(ctx)
    assert "the dentist is on friday at three" in out
    body = out.split("2026-10-19 ", 1)[1]
    assert len(body) <= 500


@pytest.mark.asyncio
async def test_recall_hit_without_the_term_is_cut_from_the_start(tmp_path):
    content = "START-MARKER " + "z" * 2000
    ctx = _make_mind_ctx(tmp_path, memsearch=_OneHit(content))
    out = await Recall(query="nothing-like-this").run(ctx)
    body = out.split("2026-10-19 ", 1)[1]
    assert body.startswith("START-MARKER") and len(body) <= 500


@pytest.mark.asyncio
async def test_short_recall_hit_is_unchanged(tmp_path):
    ctx = _make_mind_ctx(tmp_path, memsearch=_OneHit("the dentist is friday"))
    assert await Recall(query="dentist").run(ctx) == "- 2026-10-19 the dentist is friday"


@pytest.mark.asyncio
async def test_tool_message_over_8000_chars_is_cut_except_invoke_skill():
    big = "a" * 10_000

    async def dispatch(name, args):
        return ToolResult(tool_name=name, success=True, detail=big)

    calls = [
        ToolCall(id="c1", name="ReadToolOutput", arguments=json.dumps({"id": "x"})),
        ToolCall(id="c2", name="InvokeSkill", arguments=json.dumps({"name": "x"})),
    ]
    cut, skill = await run_tool_calls(calls, dispatch)
    assert cut.message["content"] == "a" * 8000 + "\n(cut; 10000 chars total)"
    assert skill.message["content"] == big
    assert cut.result.detail == big  # only the message is cut
