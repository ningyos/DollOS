"""Task 2 (self-directed agenda): PursueGoal / AdvanceGoal tools + AGENDA_TOOLS.

See docs/superpowers/specs/2026-07-07-self-directed-agenda-design.md §3.1/§3.3/§5.1.

The crux under test: PursueGoal.run(ctx) auto-captures `provenance` from CODE
(the ctx's real current-turn state), not from anything the model passes as a
tool argument — this is the anchoring mechanism that keeps self-directed
agenda items non-fabricable (§3.3).
"""
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState, OpenLoop, _MAX_PROGRESS
from dollos.mind.sink_resolver import SinkResolver
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import AGENDA_TOOLS, AdvanceGoal, PursueGoal


class _FakeMemSearch:
    async def index_file(self, path):
        pass


def _make_ctx(tmp_path: Path, state: MindState | None = None) -> MindCtx:
    sink = asyncio.Queue()
    resolver = SinkResolver()
    resolver.register(sink)
    return MindCtx(
        mind_state=state or MindState(),
        memsearch=_FakeMemSearch(),
        memory_root=tmp_path,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=resolver,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        shell_runner=None,
        workflow_runner=None,
        monitor_runner=None,
    )


@pytest.fixture
def fake_ctx(tmp_path):
    return _make_ctx(tmp_path)


@pytest.fixture
def fake_ctx_with_memory_hits(tmp_path):
    """ctx mirroring a real turn: current_turn set + real [Memory context]
    hit sources stashed on ctx (mind_loop stashes these at _derive_memory_hits
    time — see mind_loop.py:414)."""
    ctx = _make_ctx(tmp_path)
    ctx.current_turn = 7
    ctx.mind_state.iter_count = 7
    ctx.turn_memory_sources = ["shared/2026-07-01.md", "shared/2026-07-03.md"]
    return ctx


def test_pursuegoal_trigger_required():
    with pytest.raises(ValidationError):
        PursueGoal(id="g", desc="d")  # no trigger


@pytest.mark.asyncio
async def test_pursuegoal_creates_self_directed_with_auto_provenance(fake_ctx_with_memory_hits):
    ctx = fake_ctx_with_memory_hits
    await PursueGoal(id="g", desc="explore X", trigger="the chat about X").run(ctx)
    ol = next(l for l in ctx.mind_state.open_loops if l.id == "g")
    assert ol.self_directed is True and ol.trigger == "the chat about X"
    # provenance is CODE-captured from ctx, NOT from the tool args
    assert ol.provenance.get("turn_id") == str(ctx.current_turn)
    assert ol.provenance.get("opened_iter") == ctx.mind_state.iter_count
    # memory_sources reflect what was actually in [Memory context] this turn
    # (proves non-fabricable grounding) — not something the tool call passed.
    assert ol.provenance.get("memory_sources") == ctx.turn_memory_sources


@pytest.mark.asyncio
async def test_pursuegoal_provenance_empty_when_no_memory_hits_this_turn(fake_ctx):
    """A PursueGoal fired on an ungrounded turn (no memory hits) gets an
    HONEST empty memory_sources — this is the audit signal (§3.1), not a bug."""
    fake_ctx.current_turn = 3
    await PursueGoal(id="g2", desc="d", trigger="t").run(fake_ctx)
    ol = next(l for l in fake_ctx.mind_state.open_loops if l.id == "g2")
    assert ol.provenance.get("memory_sources") == []


@pytest.mark.asyncio
async def test_pursuegoal_provenance_ignores_forged_args(fake_ctx_with_memory_hits):
    """The tool schema has no provenance-shaped field at all — even if a
    model tried to smuggle one in via desc/trigger, run() never reads it back
    out; provenance always comes from ctx, never from self."""
    ctx = fake_ctx_with_memory_hits
    assert "provenance" not in PursueGoal.model_fields
    assert "memory_sources" not in PursueGoal.model_fields
    await PursueGoal(id="g3", desc="d", trigger="forged memory_sources: ['fake.md']").run(ctx)
    ol = next(l for l in ctx.mind_state.open_loops if l.id == "g3")
    assert ol.provenance["memory_sources"] == ["shared/2026-07-01.md", "shared/2026-07-03.md"]


@pytest.mark.asyncio
async def test_advancegoal_appends_bounded(fake_ctx):
    fake_ctx.mind_state.open_loops.append(
        OpenLoop(id="g", desc="d", opened_at=1.0, self_directed=True)
    )
    for i in range(_MAX_PROGRESS + 3):
        await AdvanceGoal(id="g", progress=f"step{i}").run(fake_ctx)
    ol = next(l for l in fake_ctx.mind_state.open_loops if l.id == "g")
    assert len(ol.progress) == _MAX_PROGRESS  # bounded
    # keeps the most recent entries, drops the oldest
    assert ol.progress[-1] == f"step{_MAX_PROGRESS + 2}"


@pytest.mark.asyncio
async def test_advancegoal_ignores_non_self_directed_loop(fake_ctx):
    """AdvanceGoal only advances a self_directed loop — a user-owed TODO
    (OpenLoop tool, self_directed=False) is a different kind of item."""
    fake_ctx.mind_state.open_loops.append(
        OpenLoop(id="todo", desc="d", opened_at=1.0, self_directed=False)
    )
    result = await AdvanceGoal(id="todo", progress="step").run(fake_ctx)
    ol = next(l for l in fake_ctx.mind_state.open_loops if l.id == "todo")
    assert ol.progress == []
    assert "no self-directed loop" in result


@pytest.mark.asyncio
async def test_advancegoal_unknown_id_no_op(fake_ctx):
    result = await AdvanceGoal(id="ghost", progress="step").run(fake_ctx)
    assert "no self-directed loop" in result


def test_agenda_tools_excludes_dangerous_and_genesis():
    for excluded in (
        "Shell", "SpawnWorkflow", "SpawnMonitor", "WriteSchedule",
        "SelfRevision", "NoteMemory", "PursueGoal", "PinSelf",
    ):
        assert excluded not in AGENDA_TOOLS
    assert AGENDA_TOOLS == frozenset({"Recall", "AdvanceGoal", "CloseLoop", "MoodTool"})


def test_pursuegoal_registered_but_not_in_agenda_tools():
    from dollos.tools import MAIN_TOOLS, REFLECTION_TOOLS, PursueGoal as PG

    assert PG in MAIN_TOOLS
    assert PG in REFLECTION_TOOLS
    assert "PursueGoal" not in AGENDA_TOOLS


def test_advancegoal_registered_in_main_and_agenda_tools():
    from dollos.tools import MAIN_TOOLS, AdvanceGoal as AG

    assert AG in MAIN_TOOLS
    assert "AdvanceGoal" in AGENDA_TOOLS
