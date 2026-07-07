"""Task 7 — dedicated DiaryMoment turn: narrowed registry, injected
[Today's log], suppressed outbound speech + doll-transcript, I1
no-fabrication post-turn guarantee (warn + observable marker + one retry).

Assertion APIs adjusted to the real code (see comments below) — the brief's
draft assertions used APIs that don't exist on the real objects:
  - ``PerceptionQueue`` has no public ``qsize()`` — the real pattern (already
    used by tests/test_p1c_integration.py) is ``queue._queue.qsize()``.
  - ``MindCtx.sink`` is ALWAYS ``None`` (see mind_ctx.py — it's a cascade-compat
    field, not the streaming output channel). The real streaming sink is
    registered into ``ctx.sink_resolver`` via ``make_mindloop(sink=...)`` and
    drained directly (it's the same asyncio.Queue object).
  - The brief's stream for the suppression test placed the "naked" sentence
    INSIDE the ``<think>...</think>`` block. ``ToolStreamParser`` in voice
    mode discards ALL think-block content unconditionally (IN_THINK state) —
    that placement would pass even with zero suppression logic implemented.
    Moved the sentence to AFTER ``</think>`` (the actual "speech" region) so
    the test genuinely exercises ``_emit_sentence``'s ``_is_diary`` gate.
"""
import asyncio
import time
from datetime import date

import pytest

from dollos.mind.mind_state import MindState, OutputRecord, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _drain
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM


def _seed_today_log(tmp_path, text):
    f = tmp_path / "memory" / "transcripts" / f"{date.today():%Y-%m-%d}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_diary_turn_narrows_tools_and_injects_today_log(tmp_path):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n- 12:00:00 ▸ 我跑了指令 ls\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    captured = {}
    stream = (
        'SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: WriteDiary\n</think>\n\n'
        '<tool_call>\n{"name":"WriteDiary","arguments":{"content":"今天跑了點東西"}}\n</tool_call>'
    )
    ml = make_mindloop(
        memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream),
    )
    # capture the active registry on the diary turn
    orig = ml._active_tool_registry
    ml._active_tool_registry = lambda: (captured.__setitem__("reg", set(orig().keys())) or orig())
    # capture render_mind's kwargs to verify the full day's log was injected
    import dollos.mind.mind_loop as mind_loop_mod
    orig_render_mind = mind_loop_mod.render_mind

    def _spy_render_mind(*args, **kwargs):
        captured["today_log_block"] = kwargs.get("today_log_block")
        return orig_render_mind(*args, **kwargs)

    mind_loop_mod.render_mind = _spy_render_mind
    try:
        await ml.iterate()
    finally:
        mind_loop_mod.render_mind = orig_render_mind
    assert ml._is_diary is True
    assert captured["reg"] == {"WriteDiary", "Recall"}     # narrowed
    # the day's full action log made it into the composed prompt (the whole
    # point of the dedicated turn — spec §2.2).
    assert captured["today_log_block"] is not None
    assert "12:00:00" in captured["today_log_block"]
    assert "我跑了指令 ls" in captured["today_log_block"]


@pytest.mark.asyncio
async def test_diary_turn_suppresses_outbound_speech(tmp_path):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    sink: asyncio.Queue = asyncio.Queue()
    # She streams naked text AFTER </think> (the actual speech region) on the
    # diary turn — it must NOT reach the sink.
    stream = (
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: WriteDiary\n</think>\n\n"
        "這是我不該說出口的碎念"
        '<tool_call>\n{"name":"WriteDiary","arguments":{"content":"ok"}}\n</tool_call>'
    )
    ml = make_mindloop(
        memory_root=tmp_path / "memory", state=state, queue=queue,
        llm=_FakeLLM(stream), sink=sink,
    )
    await ml.iterate()
    out = await _drain(sink)
    # Only the turn-end sentinel (None) — the naked sentence never broadcast.
    assert out == [None]


@pytest.mark.asyncio
async def test_diary_miss_warns_and_reenqueues_once(tmp_path, caplog):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    # she does NOT call WriteDiary (ends with TOOL: none, no tool_call at all)
    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\n"
    ml = make_mindloop(
        memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream),
    )
    with caplog.at_level("WARNING"):
        await ml.iterate()
    assert any(
        "diary" in r.message.lower() and "no WriteDiary" in r.message
        for r in caplog.records
    )
    # Observable marker in recent_outputs (audit/trace surface — I1).
    assert any(
        isinstance(o, OutputRecord) and o.kind == "DiaryMissed"
        for o in state.recent_outputs
    )
    # one retry DiaryMoment re-enqueued (real API: PerceptionQueue wraps a
    # plain asyncio.Queue as `_queue`, no public qsize()).
    assert ml._queue._queue.qsize() >= 1
    first_retry_qsize = ml._queue._queue.qsize()

    # simulate the retry turn also missing — must NOT enqueue a second time
    # (per-day flag).
    with caplog.at_level("WARNING"):
        await ml.iterate()
    assert ml._queue._queue.qsize() <= first_retry_qsize   # did not grow again
