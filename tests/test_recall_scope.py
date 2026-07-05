"""P1e Task 4 (S3): external_public retrieval scope.

On an external_public (stranger) turn:
  (a) the auto-injected ``[Memory context]`` is suppressed entirely
      (``MindLoop._derive_memory_hits`` returns ``[]``).
  (b) explicit ``Recall`` scopes to ONLY the ``external_public/`` tier at the
      SQL layer (a fail-closed ALLOWLIST — whole-branch review C2), so a
      stranger's turn can never surface owner-private memory, INCLUDING
      tiers that are not ``shared/``/``external_dm/`` (e.g. ``transcripts/``
      — the C2 finding: the old denylist only enumerated
      ``{shared, external_dm}`` and silently missed ``transcripts/``).

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
    # Recall's source_prefix allowlist computes ctx.memory_root/"external_public".
    # transcripts/ is included here (mirroring kernel.build_memsearch, post-I3)
    # so the C2 teeth tests below can prove transcripts/ never leaks through
    # Recall on an external_public turn.
    base = tmp_path
    shared = base / "shared"
    public = base / "external_public"
    dm = base / "external_dm"
    transcripts = base / "transcripts"
    shared.mkdir(parents=True)
    public.mkdir(parents=True)
    dm.mkdir(parents=True)
    transcripts.mkdir(parents=True)
    ms = FtsMemory(
        paths=[str(shared), str(public), str(dm), str(transcripts)],
        db_path=base / "fts.db",
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


# ---------------------------------------------------------------------------
# Whole-branch review C2: the OLD denylist only enumerated
# {shared/, external_dm/} — transcripts/ (owner+Doll verbatim conversation,
# see memory_writer.append_transcript) was NEVER in that list, so a
# stranger's Recall could pull it straight out of the FTS index. The fix
# (source_prefix=external_public/ allowlist) structurally can't leak
# transcripts/ (or shared/, external_dm/, or any future tier) regardless of
# whether it's enumerated anywhere.
# ---------------------------------------------------------------------------


async def test_recall_external_public_excludes_transcripts(tmp_path: Path):
    """A stranger's Recall must never surface transcripts/ content, even
    though transcripts/ was never on the old private-tier denylist."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "transcripts" / "2026-07-05.md").write_text(
            "## h\n\n- 12:00:00 主人說：owner-verbatim-transcript-unique-666\n",
            encoding="utf-8",
        )
        (base / "external_public" / "y.md").write_text(
            "## h\n\npublic-fact unique-666.\n", encoding="utf-8"
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_public"

        out = await Recall(query="unique-666").run(ctx)
        assert "public-fact" in out
        assert "owner-verbatim-transcript" not in out
    finally:
        ms.close()


async def test_recall_external_public_teeth_transcripts_never_returned(tmp_path: Path):
    """Invert the assertion to prove real teeth: if the allowlist regressed
    back to the old denylist (which never enumerated transcripts/), the
    transcript marker WOULD surface and this ``assert`` would NOT raise."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "transcripts" / "2026-07-05.md").write_text(
            "## h\n\n- 12:00:00 主人說：owner-verbatim-transcript-unique-777\n",
            encoding="utf-8",
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_public"

        out = await Recall(query="unique-777").run(ctx)
        with pytest.raises(AssertionError):
            assert "owner-verbatim-transcript" in out
    finally:
        ms.close()


async def test_recall_internal_turn_transcripts_still_retrievable_control(tmp_path: Path):
    """Control: internal (owner) turns are unrestricted — transcripts/ content
    IS retrievable via Recall same as before this fix."""
    ms, base = _make_memsearch(tmp_path)
    try:
        (base / "transcripts" / "2026-07-05.md").write_text(
            "## h\n\n- 12:00:00 主人說：owner-verbatim-transcript-unique-888\n",
            encoding="utf-8",
        )
        await ms.index()

        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        assert ctx.origin_tier == "internal"

        out = await Recall(query="unique-888").run(ctx)
        assert "owner-verbatim-transcript" in out
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# (c) tool_habits_search — the 5th retrieval channel (review fix)
#
# shared/tool_playbook.md lives inside the PRIVATE tier and its hits render
# unconditionally into the [Tool habits] prompt block. On an external_public
# turn this is a silent private leak unless the tool_habits_search call is
# gated. These tests drive a real MindLoop.iterate() and inspect the rendered
# prompt the LLM actually received (the true leak surface).
# ---------------------------------------------------------------------------


# The secret marker lives ONLY in the playbook lesson BODY — never in focus /
# tool_stats (those render into the prompt directly and would pollute the
# leak assertion). A separate, non-secret query word (_HABIT_QUERY) appears in
# both focus and the playbook situation line so tool_habits_search actually
# matches; the marker rides along in the lesson body and can only reach the
# prompt via the [Tool habits] render path.
_HABIT_MARKER = "unique-habit-marker-555"
_HABIT_QUERY = "greppyword"


class _CapturingLLM:
    """Records the ``user`` prompt of every stream_completion call. Yields a
    single-pass ``TOOL: none`` stream so the turn converges without a re-feed
    (mirrors tests/test_mind_loop.py::_FakeLLM's convergence contract)."""

    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None, purpose="cascade"
    ):
        self.user_prompts.append(user)

        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        yield _Chunk(text="SEEN: x\nTOOL: none\n</think>\n\nhi", done=True)

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None,
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _seed_playbook(base: Path) -> None:
    """Write shared/tool_playbook.md with a uniquely-marked lesson. The
    situation line carries the (non-secret) query word so tool_habits_search
    matches; the secret marker is in the lesson body so it can only reach the
    prompt through the [Tool habits] render path."""
    (base / "shared" / "tool_playbook.md").write_text(
        f"## 2026-06-27 10:00:00\n\n[situation] {_HABIT_QUERY} handling\nlesson {_HABIT_MARKER}\n",
        encoding="utf-8",
    )


def _loop_with_capture(tmp_path, ms, state, ctx, llm):
    return MindLoop(
        state=state,
        queue=PerceptionQueue(),
        ctx=ctx,
        llm=llm,
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )


async def test_tool_habits_leaked_on_internal_turn_control(tmp_path: Path):
    """Control: on an internal turn the [Tool habits] block DOES surface the
    playbook lesson — proving the marker is retrievable when not gated."""
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed_playbook(base)
        await ms.index()

        state = MindState()
        state.tool_stats = {"Shell": {"ok": 1, "fail": 0}}
        state.focus = f"{_HABIT_QUERY} handling"
        state.recent_perceptions.append(
            Perception(kind="UserSpoke", t=1.0, data={"text": "hi"})
        )
        import asyncio

        sink: asyncio.Queue = asyncio.Queue()
        ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
        llm = _CapturingLLM()
        loop = _loop_with_capture(tmp_path, ms, state, ctx, llm)
        loop._queue.put(Perception(kind="UserSpoke", t=2.0, data={"text": "hi"}))

        await loop.iterate()

        assert ctx.origin_tier == "internal"
        joined = "\n".join(llm.user_prompts)
        assert _HABIT_MARKER in joined
    finally:
        ms.close()


async def test_tool_habits_gated_on_external_public_turn(tmp_path: Path):
    """Teeth: on an external_public (stranger) turn the tool playbook must NOT
    reach the prompt. If the origin_tier gate on tool_habits_search were
    removed, the marker would render into [Tool habits] and this assertion
    would fail."""
    import asyncio

    ms, base = _make_memsearch(tmp_path)
    try:
        _seed_playbook(base)
        await ms.index()

        state = MindState()
        state.tool_stats = {"Shell": {"ok": 1, "fail": 0}}
        state.focus = f"{_HABIT_QUERY} handling"

        ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
        sink: asyncio.Queue = asyncio.Queue()
        ctx.sink_resolver.register(sink, locus="external", channel_id="pub")
        llm = _CapturingLLM()
        loop = _loop_with_capture(tmp_path, ms, state, ctx, llm)
        loop._queue.put(Perception(
            kind="ChannelMessage",
            t=2.0,
            data={"channel_id": "pub", "text": "hi stranger", "author_is_owner": False},
        ))

        await loop.iterate()

        assert ctx.origin_tier == "external_public"
        assert llm.user_prompts, "LLM was never called — turn did not run"
        joined = "\n".join(llm.user_prompts)
        assert _HABIT_MARKER not in joined
    finally:
        ms.close()
