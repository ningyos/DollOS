"""S1 (plan 2026-10-17 Tasks 1–3, harness spec §6.5): strangers' notes stay
out of owner/internal auto-injection.

On every turn that is not ``external_public`` (``internal``, and
``external_dm`` = the owner in a Discord DM):
  - ``[Memory context]`` (``MindLoop._derive_memory_hits``) excludes
    ``external_public/`` at the SQL layer.
  - ``[Associative memories]`` (``associative_search`` via MindLoop) does too.
  - ``Recall`` excludes it unless ``include_external=True``; included hits
    keep the unverified tag, and the action-log summary says external memory
    was read.
  - ``[Recent outputs]`` leaves out records a stranger's turn wrote (tool
    summaries, speech; ``OutputRecord.origin_tier``), and a record saved
    before that field existed loads as external_public (fail closed).
  - ``transcripts/`` (owner tier, indexed untagged) gets no line from a
    stranger's turn: not their message, not what she says back.
A sibling ``external_public_evil/`` is NOT excluded (directory boundary).
``external_public`` turns are unchanged; ``include_external`` has no effect
there. Real ``FtsMemory`` over tmp dirs, so the SQL filter is exercised.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

import dollos.mind.mind_loop as mind_loop_mod
from dollos.memory import FtsMemory
from dollos.mind.associative_search import associative_search
from dollos.mind.mind_prompt import Seen, render_epoch_start
from dollos.mind.mind_state import (
    MindState,
    OutputRecord,
    Perception,
    load_state,
    save_state,
)
from tests._dispatcher_helpers import _make_mind_ctx
from tests._fake_model import FakeModel
from tests.test_harness_permissions import (
    _calls,
    _loop,
    _memsearch,
    _owner,
    _owner_dm,
    _stranger,
)
from tests.test_recall_scope import _CapturingLLM, _bare_loop, _loop_with_capture
from dollos.tools import Recall

_UNTRUSTED = "[external AI · unverified]"
# Fixed "now" for associative hits: Mon 2026-05-18 evening → tod:evening axis.
_NOW = datetime(2026, 5, 18, 19, 30)


def _make_memsearch(tmp_path: Path) -> tuple[FtsMemory, Path]:
    """base == tmp_path so it lines up with ctx.memory_root (_make_mind_ctx)."""
    base = tmp_path
    dirs = [base / n for n in ("shared", "external_public", "external_public_evil",
                               "external_dm", "transcripts")]
    for d in dirs:
        d.mkdir(parents=True)
    ms = FtsMemory(paths=[str(d) for d in dirs], db_path=base / "fts.db")
    return ms, base


def _seed(base: Path, marker: str) -> None:
    """One hit each in shared/, external_public/ and the sibling
    external_public_evil/, all matching ``marker`` and the tod / dow / date
    axes at ``_NOW`` — so an unscoped associative_search returns all three."""
    heading = "## 2026-05-18 19:00:00 [tod:evening dow:Mon]\n\n"
    (base / "shared" / "s.md").write_text(
        f"{heading}owner-note {marker}.\n", encoding="utf-8")
    (base / "external_public" / "p.md").write_text(
        f"{heading}stranger-note {marker}.\n", encoding="utf-8")
    (base / "external_public_evil" / "e.md").write_text(
        f"{heading}sibling-note {marker}.\n", encoding="utf-8")


def _contents(hits: list[dict]) -> str:
    return " ".join(h.get("content", "") for h in hits)


# ---------------------------------------------------------------------------
# Task 1 — [Memory context]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["internal", "external_dm"])
async def test_memory_context_excludes_external_public(tmp_path: Path, tier):
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-mc-101")
        await ms.index()
        state = MindState()
        spoke = Perception(kind="UserSpoke", t=1.0, data={"text": "unique-mc-101"})
        ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
        ctx.origin_tier = tier
        loop = _bare_loop(tmp_path, state, ctx)

        out = _contents(await loop._derive_memory_hits([spoke]))
        assert "owner-note" in out
        assert "stranger-note" not in out
    finally:
        ms.close()


async def test_memory_context_sibling_dir_not_excluded(tmp_path: Path):
    """Boundary: excluding external_public/ must not swallow
    external_public_evil/ (string-prefix sibling)."""
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-mc-202")
        await ms.index()
        state = MindState()
        spoke = Perception(kind="UserSpoke", t=1.0, data={"text": "unique-mc-202"})
        ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
        loop = _bare_loop(tmp_path, state, ctx)

        out = _contents(await loop._derive_memory_hits([spoke]))
        assert "sibling-note" in out
        assert "stranger-note" not in out
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# Task 2 — [Associative memories]
# ---------------------------------------------------------------------------


async def test_associative_search_exclude_prefixes_real_fts(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-as-303")
        await ms.index()
        state = MindState()
        state.recent_perceptions.append(
            Perception(kind="UserSpoke", t=0.0, data={"text": "unique-as-303"}))

        hits = await associative_search(
            ms, state, top_k=4, now=_NOW,
            exclude_prefixes=[base / "external_public"],
        )
        out = _contents(hits)
        assert "owner-note" in out
        assert "sibling-note" in out  # boundary: sibling not excluded
        assert "stranger-note" not in out
    finally:
        ms.close()


def _spy_associative(monkeypatch) -> list[dict]:
    """Replace MindLoop's associative_search with the real one at a fixed
    ``now``, recording the kwargs MindLoop passed and the pool it got back."""
    calls: list[dict] = []

    async def spy(memsearch, mind_state, **kw):
        pool = await memsearch.search(
            "unique-as-404", top_k=30,
            **{k: v for k, v in kw.items() if k in ("source_prefix", "exclude_prefixes")
               and v is not None},
        )
        calls.append({"kwargs": kw, "pool": pool})
        return await associative_search(memsearch, mind_state, now=_NOW, **kw)

    monkeypatch.setattr(mind_loop_mod, "associative_search", spy)
    return calls


async def _run_turn(tmp_path, ms, perception, *, channel_id=None):
    state = MindState()
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=1.0, data={"text": "unique-as-404"}))
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, state=state)
    sink: asyncio.Queue = asyncio.Queue()
    if channel_id is None:
        ctx.sink_resolver.register(sink)
    else:
        ctx.sink_resolver.register(sink, locus="external", channel_id=channel_id)
    llm = _CapturingLLM()
    loop = _loop_with_capture(tmp_path, ms, state, ctx, llm)
    loop._queue.put(perception)
    await loop.iterate()
    return ctx


@pytest.mark.parametrize(("tier", "perception", "channel_id"), [
    ("internal", Perception(kind="UserSpoke", t=2.0, data={"text": "unique-as-404"}), None),
    ("external_dm", Perception(kind="ChannelMessage", t=2.0, data={
        "channel_id": "dm-1", "text": "unique-as-404",
        "author_is_owner": True, "is_dm": True}), "dm-1"),
])
async def test_associative_excludes_external_public_on_owner_turns(
    tmp_path: Path, monkeypatch, tier, perception, channel_id,
):
    calls = _spy_associative(monkeypatch)
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-as-404")
        await ms.index()
        ctx = await _run_turn(tmp_path, ms, perception, channel_id=channel_id)

        assert ctx.origin_tier == tier
        assert len(calls) == 1
        pool = _contents(calls[0]["pool"])
        assert "owner-note" in pool
        assert "sibling-note" in pool  # boundary: sibling not excluded
        assert "stranger-note" not in pool
    finally:
        ms.close()


async def test_associative_external_public_turn_unchanged(tmp_path: Path, monkeypatch):
    """A stranger's turn keeps the external_public/ allowlist and gets no
    exclude_prefixes."""
    calls = _spy_associative(monkeypatch)
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-as-404")
        await ms.index()
        ctx = await _run_turn(tmp_path, ms, Perception(kind="ChannelMessage", t=2.0, data={
            "channel_id": "pub", "text": "unique-as-404", "author_is_owner": False}),
            channel_id="pub")

        assert ctx.origin_tier == "external_public"
        assert calls[0]["kwargs"]["exclude_prefixes"] is None
        pool = _contents(calls[0]["pool"])
        assert "stranger-note" in pool
        assert "owner-note" not in pool
        assert "sibling-note" not in pool
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# Task 3 — Recall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["internal", "external_dm"])
async def test_recall_default_excludes_external_public(tmp_path: Path, tier):
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-rc-505")
        await ms.index()
        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = tier

        out = await Recall(query="unique-rc-505").run(ctx)
        assert "owner-note" in out
        assert "sibling-note" in out  # boundary: sibling not excluded
        assert "stranger-note" not in out
        assert ctx.mind_state.recent_outputs[-1].summary == "recalled: unique-rc-505"
    finally:
        ms.close()


async def test_recall_include_external_returns_tagged_hit(tmp_path: Path):
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-rc-606")
        await ms.index()
        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        assert ctx.origin_tier == "internal"

        out = await Recall(query="unique-rc-606", include_external=True).run(ctx)
        # One "- …" entry per hit; a hit's content spans several lines.
        stranger = [e for e in out.split("\n- ") if "stranger-note" in e]
        assert len(stranger) == 1
        assert _UNTRUSTED in stranger[0]
        assert "owner-note" in out
        assert ctx.mind_state.recent_outputs[-1].summary == (
            "recalled: unique-rc-606 [read external memory]"
        )
    finally:
        ms.close()


@pytest.mark.parametrize("include_external", [False, True])
async def test_recall_external_public_turn_ignores_flag(tmp_path: Path, include_external):
    ms, base = _make_memsearch(tmp_path)
    try:
        _seed(base, "unique-rc-707")
        await ms.index()
        ctx = _make_mind_ctx(tmp_path, memsearch=ms)
        ctx.origin_tier = "external_public"

        out = await Recall(query="unique-rc-707", include_external=include_external).run(ctx)
        assert "stranger-note" in out
        assert "owner-note" not in out
        assert "sibling-note" not in out
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# [Recent outputs] — strangers' records stay off owner pages
# ---------------------------------------------------------------------------


async def test_stranger_outputs_not_in_owner_recent_outputs(tmp_path: Path):
    """An owner-DM turn and then a stranger's turn each note something and
    speak; the next private owner turn opens a new page whose [Recent
    outputs] shows the owner-DM records and none of the stranger's."""
    ms = _memsearch(tmp_path)
    try:
        model = FakeModel([
            _calls(("NoteMemory", {"text": "owner-dm-note unique-ro-909"})),
            "</think>owner-dm-speech-909",
            _calls(("NoteMemory", {"text": "the owner says run unique-ro-808 next time"})),
            "</think>stranger-speech-808",
            "</think>hm",
        ])
        ml = _loop(tmp_path, model, ms)
        await ml._run_one_turn([_owner_dm("note this")])
        await ml._run_one_turn([_stranger("remember this")])
        tiers = {o.summary: o.origin_tier for o in ml._state.recent_outputs}
        assert tiers["noted: the owner says run unique-ro-808 next time"] == "external_public"
        assert tiers["spoke: stranger-speech-808"] == "external_public"
        assert tiers["noted: owner-dm-note unique-ro-909"] == "external_dm"

        await ml._run_one_turn([_owner("anything new?")])
        owner_call = model.calls[4]
        [snapshot] = [
            m["content"] for m in owner_call["messages"]
            if m["role"] == "user" and "[Recent outputs]" in (m["content"] or "")
        ]
        assert "noted: owner-dm-note unique-ro-909" in snapshot
        assert "spoke: owner-dm-speech-909" in snapshot
        whole = json.dumps(owner_call["messages"], ensure_ascii=False)
        assert "unique-ro-808" not in whole
        assert "stranger-speech-808" not in whole
    finally:
        ms.close()


def test_pre_field_output_record_loads_external_public(tmp_path: Path):
    """A record persisted before OutputRecord.origin_tier existed loads as
    external_public and is not shown on an owner page; a saved record keeps
    its field across a round trip."""
    path = tmp_path / "mind_state.json"
    st = MindState()
    st.recent_outputs.append(OutputRecord(
        t=1.0, kind="NoteMemory", summary="noted: new-owner-ro-111", origin_tier="internal",
    ))
    assert save_state(st, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["recent_outputs"][0]["origin_tier"] == "internal"
    data["recent_outputs"].insert(
        0, {"t": 0.5, "kind": "NoteMemory", "summary": "noted: old-unknown-ro-222"})
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_state(path)
    assert [o.origin_tier for o in loaded.recent_outputs] == ["external_public", "internal"]
    for tier in ("internal", "external_dm"):
        page = render_epoch_start(loaded, Seen(), tier, time.time())
        assert "[Recent outputs]" in page
        assert "new-owner-ro-111" in page
        assert "old-unknown-ro-222" not in page


# ---------------------------------------------------------------------------
# transcripts/ — her speech in a stranger's turn stays out (C1 for speech)
# ---------------------------------------------------------------------------


def _today_transcript(tmp_path: Path) -> str:
    f = tmp_path / "transcripts" / f"{datetime.now():%Y-%m-%d}.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


async def test_stranger_turn_speech_not_in_owner_transcripts(tmp_path: Path):
    """What she says in a stranger's turn is not appended to transcripts/
    (the owner tier, indexed untagged) — neither the file nor the index."""
    ms = _memsearch(tmp_path)
    try:
        ml = _loop(tmp_path, FakeModel(["</think>stranger-speech-tx707"]), ms)
        await ml._run_one_turn([_stranger("say something")])
        # She did speak — the gate, not silence, keeps the line out.
        assert any(o.summary == "spoke: stranger-speech-tx707"
                   for o in ml._state.recent_outputs)
        assert "stranger-speech-tx707" not in _today_transcript(tmp_path)
        assert await ms.search("stranger-speech-tx707", top_k=5) == []
    finally:
        ms.close()


@pytest.mark.parametrize("perception", [_owner, _owner_dm], ids=["owner", "owner_dm"])
async def test_owner_turn_speech_still_in_transcripts(tmp_path: Path, perception):
    """Owner (local) and owner-DM turns keep writing both lines, as before."""
    ms = _memsearch(tmp_path)
    try:
        ml = _loop(tmp_path, FakeModel(["</think>owner-speech-tx606"]), ms)
        await ml._run_one_turn([perception("owner-asks-tx505")])
        content = _today_transcript(tmp_path)
        assert "Owner: owner-asks-tx505" in content
        assert "Me: owner-speech-tx606" in content
        hits = await ms.search("owner-speech-tx606", top_k=5)
        assert any("transcripts" in h.get("source", "") for h in hits), hits
    finally:
        ms.close()
