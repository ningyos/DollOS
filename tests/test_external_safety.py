"""P1e Task 6: 4-way split integration test.

This is the regression net against "piecemeal missed a gate" — the plan's
authoritative split table (`docs/superpowers/plans/2026-07-03-p1e-external-safety.md`,
"4-way split 權威表") lists five axes (tool registry / energy / last_user_at /
memory write routing / memory retrieval scope) that Tasks 1-5 each touched
one at a time. Each task's own focused test proves its axis in isolation;
this file drives one REAL turn per tier through `MindLoop._run_one_turn`
(with a real `FtsMemory` — not mocked) and asserts every axis at once, so a
future change that regresses any single gate breaks here even if it doesn't
break that gate's own dedicated test file.

Split table (internal | external_dm=owner DM | external_public=stranger):
  - tool registry:      full        | conservative (no Shell) | conservative (no Shell)
  - energy consumption: drains      | drains (upgraded)       | does NOT drain
  - last_user_at:       advances    | advances (upgraded)     | does NOT advance
  - memory write dir:   shared/     | external_dm/            | external_public/
  - memory retrieval:   full        | full (owner sees own)   | private tier excluded
                                                                 + auto-context suppressed
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dollos.memory import FtsMemory
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS, Recall
from tests._dispatcher_helpers import _make_mind_ctx
from tests.test_mind_loop import _FakeLLM, _note_memory_pass


def _make_memsearch(tmp_path: Path) -> tuple[FtsMemory, Path]:
    """Real FtsMemory over tmp dirs, mirroring tests/test_recall_scope.py's
    harness — base == tmp_path so it lines up with ctx.memory_root (Recall's
    _private_tier_prefixes(ctx) computes ctx.memory_root/"shared" etc.)."""
    base = tmp_path
    dirs = [base / "shared", base / "external_public", base / "external_dm"]
    for d in dirs:
        d.mkdir(parents=True)
    ms = FtsMemory(paths=[str(d) for d in dirs], db_path=base / "fts.db")
    return ms, base


def _make_loop(tmp_path: Path, ms: FtsMemory, *, llm=None) -> tuple[MindLoop, object, MindState]:
    state = MindState()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
    loop = MindLoop(
        state=state,
        queue=PerceptionQueue(),
        ctx=ctx,
        llm=llm or _FakeLLM(_note_memory_pass()),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
        energy_enabled=True,
        cost_per_turn=0.1,
    )
    return loop, ctx, state


def _channel_msg(content: str, *, author_is_owner: bool, t: float,
                  channel_id: str = "disc:g1:c1") -> Perception:
    return Perception(
        kind="ChannelMessage",
        t=t,
        data={"content": content, "channel_id": channel_id,
              "author_is_owner": author_is_owner},
    )


def _user_perception(text: str, *, t: float) -> Perception:
    return Perception(kind="UserSpoke", t=t, data={"text": text})


# ---------------------------------------------------------------------------
# external_public (stranger)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_public_turn_4way_split(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "secret.md").write_text(
            "## h\n\nowner-private-fact-pub-check.\n", encoding="utf-8"
        )
        await ms.index()

        loop, ctx, state = _make_loop(
            tmp_path, ms, llm=_FakeLLM(_note_memory_pass(text="stranger said hi"))
        )
        await loop._run_one_turn(
            [_channel_msg("hi", author_is_owner=False, t=1.0)]
        )

        # (a) conservative tool registry — no Shell
        reg = loop._active_tool_registry()
        assert "Shell" not in reg
        assert set(reg.keys()) <= {"Recall", "NoteMemory", "WriteDiary", "PinSelf"}

        # (b) energy NOT drained
        assert state.energy == pytest.approx(1.0)

        # (c) NoteMemory wrote to external_public/, not shared/
        expected = tmp_path / "external_public" / f"{date.today():%Y-%m-%d}.md"
        assert expected.exists()
        assert "stranger said hi" in expected.read_text()
        not_expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
        assert not not_expected.exists()

        # (d) auto-[Memory context] suppressed
        hits = await loop._derive_memory_hits()
        assert hits == []

        # (d2) explicit Recall also excludes the private tier on this turn
        out = await Recall(query="owner-private-fact-pub-check").run(ctx)
        assert "owner-private-fact" not in out

        # (e) last_user_at NOT advanced
        assert state.last_user_at == pytest.approx(0.0)
        assert state.user_turn_count == 0
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# external_dm (owner DM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_dm_owner_turn_4way_split(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "secret.md").write_text(
            "## h\n\nowner-private-fact-dm-check.\n", encoding="utf-8"
        )
        await ms.index()

        loop, ctx, state = _make_loop(
            tmp_path, ms, llm=_FakeLLM(_note_memory_pass(text="owner dm note"))
        )
        t = 12345.0
        await loop._run_one_turn(
            [_channel_msg("hi", author_is_owner=True, t=t)]
        )

        # (a) conservative tool registry — no Shell (owner-DM is not RCE)
        reg = loop._active_tool_registry()
        assert "Shell" not in reg
        assert set(reg.keys()) <= {"Recall", "NoteMemory", "WriteDiary", "PinSelf"}

        # (b) energy DRAINED (upgraded)
        assert state.energy == pytest.approx(0.9)

        # (c) last_user_at ADVANCED (upgraded)
        assert state.last_user_at == pytest.approx(t)
        assert state.user_turn_count == 1

        # NoteMemory wrote to external_dm/ (write-side routing, for parity)
        expected = tmp_path / "external_dm" / f"{date.today():%Y-%m-%d}.md"
        assert expected.exists()
        assert "owner dm note" in expected.read_text()

        # (d) retrieval NOT scoped — owner sees her own private shared/ note
        out = await Recall(query="owner-private-fact-dm-check").run(ctx)
        assert "owner-private-fact" in out
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# internal (local chat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_turn_4way_split(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "secret.md").write_text(
            "## h\n\nowner-private-fact-internal-check.\n", encoding="utf-8"
        )
        await ms.index()

        loop, ctx, state = _make_loop(
            tmp_path, ms, llm=_FakeLLM(_note_memory_pass(text="internal note"))
        )
        t = 555.0
        await loop._run_one_turn([_user_perception("hi", t=t)])

        # full tool registry
        reg = loop._active_tool_registry()
        assert "Shell" in reg
        assert "SpawnWorkflow" in reg
        assert "WriteSchedule" in reg

        # energy drains
        assert state.energy == pytest.approx(0.9)

        # last_user_at advances
        assert state.last_user_at == pytest.approx(t)
        assert state.user_turn_count == 1

        # NoteMemory wrote to shared/ (unchanged)
        expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
        assert expected.exists()
        assert "internal note" in expected.read_text()

        # full retrieval — owner's own private note is visible
        out = await Recall(query="owner-private-fact-internal-check").run(ctx)
        assert "owner-private-fact" in out
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# Teeth: prove the assertions aren't vacuous
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_public_teeth_private_recall_inverted(tmp_path: Path):
    """Invert the assertion: if the retrieval-scope gate were a no-op, the
    owner's private note WOULD surface via Recall on a stranger's turn, and
    this ``assert`` would NOT raise."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "secret.md").write_text(
            "## h\n\nowner-private-fact-teeth-check.\n", encoding="utf-8"
        )
        await ms.index()

        loop, ctx, state = _make_loop(tmp_path, ms)
        await loop._run_one_turn(
            [_channel_msg("hi", author_is_owner=False, t=1.0)]
        )

        out = await Recall(query="owner-private-fact-teeth-check").run(ctx)
        with pytest.raises(AssertionError):
            assert "owner-private-fact" in out
    finally:
        ms.close()


@pytest.mark.asyncio
async def test_external_public_teeth_energy_inverted(tmp_path: Path):
    """Invert the assertion: if the energy origin-guard were removed, a
    stranger's turn WOULD drain energy, and this ``assert`` would NOT raise."""
    ms, base = _make_memsearch(tmp_path)
    try:
        loop, ctx, state = _make_loop(tmp_path, ms)
        await loop._run_one_turn(
            [_channel_msg("hi", author_is_owner=False, t=1.0)]
        )
        with pytest.raises(AssertionError):
            assert state.energy == pytest.approx(0.9)
    finally:
        ms.close()
