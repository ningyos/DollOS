"""Turn kinds and their policies (harness spec §9 S3, mind-loop spec §4.3,
plan 2026-10-27 Task 3 / S3).

- ``kind_of``: safe mode first; reflection when any ReflectionMoment is in
  the batch; diary / agenda / pulse only for a PURE batch (a co-batched
  UserSpoke, or any other perception, makes it a normal turn).
- The bound / runnable / offered tool sets of every reachable (kind,
  principal) pair, written out literally. They were captured from the
  flag-based ``_active_tool_registry`` / ``_offered_tools`` at 6e1b56e,
  before this refactor; they replace Task 2's old-vs-new equivalence test.
- The deleted flags and tool lists stay deleted.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from dollos.harness.turn_policy import (
    POLICIES,
    bound_tools,
    effective_kind,
    kind_of,
    moment_of,
    offered_tools,
)
from dollos.mind.mind_state import Perception
from dollos.tools import KEEPER_TOOLS, MAIN_TOOLS, SUB_TOOLS
from tests._mindloop_factory import make_mindloop


def _p(kind: str, **data) -> Perception:
    return Perception(kind=kind, t=time.time(), data=data)


USER = _p("UserSpoke", text="hi")
OWNER_DM = _p("ChannelMessage", channel_id="dm", content="hi",
              author_is_owner=True, is_dm=True)
STRANGER = _p("ChannelMessage", channel_id="c1", content="hi",
              author_is_owner=False, is_dm=False)
REFLECT = _p("ReflectionMoment", iters_since_last=5)
DIARY = _p("DiaryMoment")
AGENDA = _p("AgendaMoment")
PULSE = _p("PulseMoment", severity="critical", detail="hot")
RESULT = _p("ToolResultArrived")
SCHEDULED = _p("ScheduledMoment")


# ---------------------------------------------------------------------------
# kind_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("batch", "kind"), [
    ([], "conversation"),
    ([USER], "conversation"),
    ([OWNER_DM], "conversation"),
    ([STRANGER], "conversation"),
    ([RESULT], "conversation"),
    ([SCHEDULED], "conversation"),
    ([REFLECT], "reflection"),
    ([REFLECT, REFLECT], "reflection"),
    ([USER, REFLECT], "reflection"),          # MF-2: owner present, still reflection
    ([REFLECT, DIARY], "reflection"),          # reflection wins over a moment
    ([REFLECT, AGENDA], "reflection"),
    ([REFLECT, PULSE], "reflection"),
    ([DIARY], "diary"),
    ([DIARY, DIARY], "diary"),
    ([AGENDA], "agenda"),
    ([AGENDA, AGENDA], "agenda"),
    ([PULSE], "pulse"),
    ([PULSE, PULSE], "pulse"),
    # A co-batched UserSpoke (or anything else) is never whitewashed into a
    # narrowed moment turn.
    ([DIARY, USER], "conversation"),
    ([USER, DIARY], "conversation"),
    ([AGENDA, USER], "conversation"),
    ([PULSE, USER], "conversation"),
    ([AGENDA, RESULT], "conversation"),
    ([AGENDA, SCHEDULED], "conversation"),
    ([DIARY, AGENDA], "conversation"),
    ([DIARY, PULSE], "conversation"),
    ([AGENDA, PULSE], "conversation"),
    ([DIARY, RESULT], "conversation"),
    ([PULSE, OWNER_DM], "conversation"),
])
def test_kind_of(batch, kind):
    assert kind_of(batch, safe_mode=False) == kind
    assert moment_of(batch) == kind


@pytest.mark.parametrize("batch", [
    [], [USER], [STRANGER], [REFLECT], [USER, REFLECT], [DIARY], [AGENDA],
    [PULSE], [DIARY, USER],
])
def test_safe_mode_has_top_priority(batch):
    assert kind_of(batch, safe_mode=True) == "safe_mode"
    assert effective_kind(moment_of(batch), safe_mode=True) == "safe_mode"


# ---------------------------------------------------------------------------
# Bound / runnable / offered sets, literally
# ---------------------------------------------------------------------------

_MAIN = frozenset({
    "AdvanceGoal", "CloseLoop", "InvokeSkill", "MoodTool",
    "NoteMemory", "OpenLoop", "PursueGoal", "ReadToolOutput", "Recall",
    "SetFocus", "Shell",
    "SpawnWorkflow", "WriteDiary", "WriteSchedule",
})
_SAFE = {"ReadToolOutput", "Recall"}
_EXTERNAL = {"NoteMemory", "Recall", "WriteDiary"}

# (kind, principal) → (bound = _active_tool_registry, runnable =
# _runnable_tools(_offered_tools()), refusal label = _turn_kind()).
# Diary / agenda / pulse are pure-batch kinds, so only internal reaches them.
_EVERYTHING_ON = [
    # PinSelf on an owner-present conversation (remember-corrections plan
    # Task 2, decision 1): owner and owner_dm, never internal.
    (("conversation", "owner"), _MAIN | {"LearnName", "PinSelf"},
     _MAIN | {"LearnName", "PinSelf"}, "owner"),
    (("reflection", "owner"),
     _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"},
     _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"}, "reflection"),
    (("safe_mode", "owner"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "owner_dm"), _MAIN | {"LearnName", "PinSelf"},
     _MAIN | {"LearnName", "PinSelf"}, "owner"),
    (("reflection", "owner_dm"),
     _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"},
     _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"}, "reflection"),
    (("safe_mode", "owner_dm"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "internal"), _MAIN, _MAIN, "normal"),
    (("reflection", "internal"),
     _MAIN | {"NoteToolLesson", "PinSelf", "SelfRevision"},
     _MAIN | {"NoteToolLesson", "PinSelf", "SelfRevision"}, "reflection"),
    (("diary", "internal"), {"Recall", "WriteDiary"}, {"Recall", "WriteDiary"}, "diary"),
    (("agenda", "internal"), {"AdvanceGoal", "CloseLoop", "MoodTool", "Recall"},
     {"AdvanceGoal", "CloseLoop", "MoodTool", "Recall"}, "agenda"),
    (("pulse", "internal"), {"MoodTool", "NoteMemory", "Recall"},
     {"MoodTool", "NoteMemory", "Recall"}, "pulse"),
    (("safe_mode", "internal"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "external_public"), _EXTERNAL, _EXTERNAL, "external_public"),
    (("reflection", "external_public"), _EXTERNAL, _EXTERNAL, "external_public"),
    (("safe_mode", "external_public"), {"Recall"}, {"Recall"}, "safe mode"),
]
_EVERYTHING_OFF = [
    (("conversation", "owner"), _MAIN | {"LearnName"}, _MAIN | {"LearnName"}, "owner"),
    (("reflection", "owner"), _MAIN | {"LearnName", "NoteToolLesson"},
     _MAIN | {"LearnName", "NoteToolLesson"}, "reflection"),
    (("safe_mode", "owner"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "owner_dm"), _MAIN | {"LearnName"}, _MAIN | {"LearnName"}, "owner"),
    (("reflection", "owner_dm"), _MAIN | {"LearnName", "NoteToolLesson"},
     _MAIN | {"LearnName", "NoteToolLesson"}, "reflection"),
    (("safe_mode", "owner_dm"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "internal"), _MAIN, _MAIN, "normal"),
    (("reflection", "internal"), _MAIN | {"NoteToolLesson"}, _MAIN | {"NoteToolLesson"},
     "reflection"),
    (("diary", "internal"), {"Recall", "WriteDiary"}, {"Recall", "WriteDiary"}, "diary"),
    (("agenda", "internal"), {"AdvanceGoal", "CloseLoop", "MoodTool", "Recall"},
     {"AdvanceGoal", "CloseLoop", "MoodTool", "Recall"}, "agenda"),
    (("pulse", "internal"), {"MoodTool", "NoteMemory", "Recall"},
     {"MoodTool", "NoteMemory", "Recall"}, "pulse"),
    (("safe_mode", "internal"), _SAFE, _SAFE, "safe mode"),
    (("conversation", "external_public"), _EXTERNAL, _EXTERNAL, "external_public"),
    (("reflection", "external_public"), _EXTERNAL, _EXTERNAL, "external_public"),
    (("safe_mode", "external_public"), {"Recall"}, {"Recall"}, "safe mode"),
]

# principal → offered (the tier's fixed list), with (self_profile, evolution).
_OFFERED = {
    (True, True): {
        "owner": _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"},
        "owner_dm": _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"},
        "internal": _MAIN | {"LearnName", "NoteToolLesson", "PinSelf", "SelfRevision"},
        "external_public": _EXTERNAL,
    },
    (False, False): {
        "owner": _MAIN | {"LearnName", "NoteToolLesson"},
        "owner_dm": _MAIN | {"LearnName", "NoteToolLesson"},
        "internal": _MAIN | {"LearnName", "NoteToolLesson"},
        "external_public": _EXTERNAL,
    },
}

_CASES = [
    pytest.param(True, True, *row, id=f"on-{row[0][0]}-{row[0][1]}")
    for row in _EVERYTHING_ON
] + [
    pytest.param(False, False, *row, id=f"off-{row[0][0]}-{row[0][1]}")
    for row in _EVERYTHING_OFF
]


def _loop(tmp_path: Path, kind: str, principal: str, self_profile: bool, evolution: bool):
    ml = make_mindloop(memory_root=tmp_path, self_profile_enabled=self_profile,
                       evolution_enabled=evolution)
    ml._ctx.principal = principal
    ml._state.safe_mode = kind == "safe_mode"
    ml._moment = "conversation" if kind == "safe_mode" else kind
    return ml


def test_main_tools_literal_is_the_registry():
    assert {c.__name__ for c in MAIN_TOOLS} == _MAIN


@pytest.mark.parametrize(
    ("self_profile", "evolution", "situation", "bound", "runnable", "label"), _CASES,
)
def test_bound_sets(tmp_path, self_profile, evolution, situation, bound, runnable, label):
    kind, principal = situation
    ml = _loop(tmp_path, kind, principal, self_profile, evolution)
    offered = ml._offered_tools()
    assert set(ml._active_tool_registry()) == bound
    assert set(ml._runnable_tools(offered)) == runnable
    assert set(offered) == _OFFERED[(self_profile, evolution)][principal]
    assert ml._turn_kind() == label
    assert bound_tools(kind, principal, _MAIN, self_profile=self_profile,
                       evolution=evolution) == bound
    assert offered_tools(principal, _MAIN, self_profile=self_profile,
                         evolution=evolution) == _OFFERED[(self_profile, evolution)][principal]


@pytest.mark.parametrize(("self_profile", "evolution"), [(True, False), (False, True)])
def test_extras_follow_their_own_switch(tmp_path, self_profile, evolution):
    ml = _loop(tmp_path, "reflection", "internal", self_profile, evolution)
    reg = set(ml._active_tool_registry())
    assert ("PinSelf" in reg) is self_profile
    assert ("SelfRevision" in reg) is evolution
    assert "NoteToolLesson" in reg


_ALL_KINDS = tuple(POLICIES)
_PRINCIPALS = ("owner", "owner_dm", "internal", "external_public")
_PINSELF_BOUND = {
    ("conversation", "owner"), ("conversation", "owner_dm"),
    ("reflection", "owner"), ("reflection", "owner_dm"), ("reflection", "internal"),
}


@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("principal", _PRINCIPALS)
def test_pinself_binds_only_on_owner_conversation_and_reflection(kind, principal):
    """Remember-corrections plan Task 2 (spec §2.1, decision 1): PinSelf is
    bound on a conversation only while the owner is present, and on
    reflection as before; never on an internal conversation, a stranger's
    turn, a diary / agenda / pulse / safe-mode turn or an agent. With
    self_profile off it is bound nowhere."""
    on = bound_tools(kind, principal, _MAIN, self_profile=True, evolution=True)
    assert ("PinSelf" in on) is ((kind, principal) in _PINSELF_BOUND)
    off = bound_tools(kind, principal, _MAIN, self_profile=False, evolution=True)
    assert "PinSelf" not in off


def test_agent_kinds_come_from_the_agent_registries():
    assert POLICIES["worker"].kind_tools == {c.__name__ for c in SUB_TOOLS}
    assert POLICIES["keeper"].kind_tools == {"Report", "Scratchpad"}
    assert POLICIES["keeper"].kind_tools == {c.__name__ for c in KEEPER_TOOLS}


# ---------------------------------------------------------------------------
# Safe mode narrows tools only: speech and blocks follow the batch's kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("moment", "spoken"), [
    ("conversation", ["hi"]), ("reflection", ["hi"]), ("pulse", ["hi"]),
    ("diary", []), ("agenda", []),
])
@pytest.mark.parametrize("safe_mode", [False, True])
def test_speech_follows_the_batch_kind(tmp_path, moment, spoken, safe_mode):
    ml = make_mindloop(memory_root=tmp_path)
    ml._moment = moment
    ml._state.safe_mode = safe_mode
    sink: asyncio.Queue = asyncio.Queue()
    ml._emit_sentence(sink, "hi")
    got = []
    while not sink.empty():
        got.append(sink.get_nowait().text)
    assert got == spoken


# ---------------------------------------------------------------------------
# The flags and the old lists are gone
# ---------------------------------------------------------------------------

_GONE = (
    "_is_reflection", "_is_diary", "_is_agenda", "_is_pulse", "_has_user_spoke",
    "AGENDA_TOOLS", "DIARY_TOOLS", "PULSE_TOOLS", "EXTERNAL_TOOLS",
    "REFLECTION_TOOLS", "SAFE_MODE_TOOLS",
)


def test_no_turn_kind_flags_or_old_tool_lists_in_src():
    src = Path(__file__).resolve().parent.parent / "src"
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, _GONE)) + r")\b")
    hits = [
        f"{path.relative_to(src)}:{n}: {line.strip()}"
        for path in sorted(src.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []
