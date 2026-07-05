"""P1e Task 4 (S3): external_public retrieval scope.

On an external_public (stranger) turn:
  (a) the auto-injected ``[Memory context]`` is suppressed entirely
      (``MindLoop._derive_memory_hits`` returns ``[]``).
  (b) explicit ``Recall`` excludes the private tier (``shared/`` +
      ``external_dm/``) at the SQL layer, so a stranger's turn can never
      surface the owner's private memory.

internal and external_dm (owner) turns are NOT scoped — the owner sees full
memory. Uses a real ``FtsMemory`` over tmp dirs so the SQL filter is
genuinely exercised, not mocked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.memory import FtsMemory
from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import Recall, MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx
from tests.test_mind_loop import _FakeLLM


def _make_memsearch(tmp_path: Path) -> tuple[FtsMemory, Path]:
    # NB: base == tmp_path (not tmp_path/"memory") so it lines up with
    # ctx.memory_root, which _make_mind_ctx sets to tmp_path directly —
    # Recall's _private_tier_prefixes(ctx) computes ctx.memory_root/"shared"
    # etc., so the indexed dirs must match that exactly.
    base = tmp_path
    shared = base / "shared"
    public = base / "external_public"
    dm = base / "external_dm"
    shared.mkdir(parents=True)
    public.mkdir(parents=True)
    dm.mkdir(parents=True)
    ms = FtsMemory(
        paths=[str(shared), str(public), str(dm)], db_path=base / "fts.db"
    )
    return ms, base


def _bare_loop(tmp_path: Path, state: MindState, ctx) -> MindLoop:
    """Minimal MindLoop for calling _derive_memory_hits directly (no iterate).
    Mirrors tests/test_consolidation.py's ``_bare_loop``."""
    queue = PerceptionQueue()
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nhi"),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )


# ---------------------------------------------------------------------------
# (a) auto-context suppression
# ---------------------------------------------------------------------------


async def test_external_public_suppresses_auto_context(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nunique-owner-fact-777.\n", encoding="utf-8"
        )
        await ms.index()

        state = MindState()
        state.recent_perceptions.append(
            Perception(kind="UserSpoke", t=1.0, data={"text": "unique-owner-fact-777"})
        )
        ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
        ctx.origin_tier = "external_public"
        loop = _bare_loop(tmp_path, state, ctx)

        hits = await loop._derive_memory_hits()
        assert hits == []
    finally:
        ms.close()


async def test_internal_turn_auto_context_not_suppressed(tmp_path: Path):
    """Control: internal turns still get the auto-[Memory context] hits."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nunique-owner-fact-888.\n", encoding="utf-8"
        )
        await ms.index()

        state = MindState()
        state.recent_perceptions.append(
            Perception(kind="UserSpoke", t=1.0, data={"text": "unique-owner-fact-888"})
        )
        ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
        assert ctx.origin_tier == "internal"
        loop = _bare_loop(tmp_path, state, ctx)

        hits = await loop._derive_memory_hits()
        assert any("unique-owner-fact-888" in h.get("content", "") for h in hits)
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# (b) Recall scoping
# ---------------------------------------------------------------------------


async def test_recall_external_public_excludes_private_note(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate-fact unique-scope-111.\n", encoding="utf-8"
        )
        (base / "external_public" / "y.md").write_text(
            "## h\n\npublic-fact unique-scope-111.\n", encoding="utf-8"
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_public"

        out = await Recall(query="unique-scope-111").run(ctx)
        assert "public-fact" in out
        assert "private-fact" not in out
    finally:
        ms.close()


async def test_recall_internal_turn_full_retrieval(tmp_path: Path):
    """Control: internal (owner) turn retrieves private notes too."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate-fact unique-scope-222.\n", encoding="utf-8"
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        assert ctx.origin_tier == "internal"

        out = await Recall(query="unique-scope-222").run(ctx)
        assert "private-fact" in out
    finally:
        ms.close()


async def test_recall_external_dm_owner_turn_full_retrieval(tmp_path: Path):
    """Control: external_dm (owner in a DM) turn is NOT scoped — owner sees
    her own private (shared/) memory even from a DM channel."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate-fact unique-scope-333.\n", encoding="utf-8"
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_dm"

        out = await Recall(query="unique-scope-333").run(ctx)
        assert "private-fact" in out
    finally:
        ms.close()


async def test_recall_external_public_teeth_private_never_returned(tmp_path: Path):
    """Invert the assertion to prove real teeth: if scoping were a no-op, the
    private note would surface and this ``assert`` would NOT raise."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate-fact unique-scope-444.\n", encoding="utf-8"
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_public"

        out = await Recall(query="unique-scope-444").run(ctx)
        with pytest.raises(AssertionError):
            assert "private-fact" in out
    finally:
        ms.close()
