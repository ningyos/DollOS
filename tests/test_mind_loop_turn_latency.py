"""Tests for turn-level think/speak/first-speak latency telemetry wired into
``MindLoop._llm_iterate`` (延遲壓縮 Part 1 Task 2).

Epoch = ``_llm_iterate`` entry. ``first_speak_ms`` is measured at the single
outbound chokepoint (``_emit_sentence``), AFTER its existing suppression
guards (whitespace-only / agenda / diary) — a suppressed sentence must never
count as "spoke"."""
from __future__ import annotations

import time

import pytest

from dollos.llm.model import End, SpeechDelta, ToolCall, ToolCalls, Usage
from dollos.mind.mind_state import Perception
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _ScriptedLLM, _speech_pass


class _CapturingRecorder:
    def __init__(self):
        self.records = []

    async def record(self, rec):
        self.records.append(rec)


def _user_perception(text: str) -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


@pytest.mark.asyncio
async def test_turn_emits_latency_record_with_first_speak(tmp_path):
    rec = _CapturingRecorder()
    llm = _ScriptedLLM([_speech_pass("你好")])
    ml = make_mindloop(memory_root=tmp_path, llm=llm, turn_latency_recorder=rec)

    await ml._run_one_turn([_user_perception("hi")])

    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.first_speak_ms is not None and r.first_speak_ms >= 0
    assert r.speak_chars > 0
    assert r.think_chars > 0
    assert r.mode == "deliberate"
    assert r.n_passes >= 1
    assert r.had_tool_call is False
    assert r.total_ms is not None and r.total_ms >= 0
    assert r.ttft_ms is None  # the fake model's End reports no TTFT
    assert r.finish_reason == "stop"
    # Whole-branch review Finding #1: chat turns must be self-identifiable
    # so Part 2 can filter ["UserSpoke"]/["ChannelMessage"] apart from idle
    # Awoke / ReflectionMoment / AgendaMoment / DiaryMoment turns.
    assert r.perception_kinds == ["UserSpoke"]
    # No cascade_logger wired in this factory call → turn_id stays None.
    assert r.turn_id is None


@pytest.mark.asyncio
async def test_ttft_and_cache_numbers_come_from_the_first_call(tmp_path):
    """ttft_ms / cache_n / prompt_n / finish_reason are the turn's FIRST model
    call's (the one the user waits on), not the last pass's."""
    rec = _CapturingRecorder()
    first = End(
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=900, cache_n=880, prompt_n=20),
        assistant_message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "SetFocus", "arguments": '{"text": "x"}'}}]},
        ttft_ms=312.5,
    )
    second = End(
        finish_reason="stop", usage=Usage(cache_n=1, prompt_n=2),
        assistant_message={"role": "assistant", "content": "ok"}, ttft_ms=9.0,
    )
    llm = FakeModel([
        [ToolCalls((ToolCall("c1", "SetFocus", '{"text": "x"}'),)), first],
        [SpeechDelta("ok"), second],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=llm, turn_latency_recorder=rec)

    await ml._run_one_turn([_user_perception("hi")])

    r = rec.records[0]
    assert r.n_passes == 2
    assert r.ttft_ms == 312.5
    assert r.cache_n == 880
    assert r.prompt_n == 20
    assert r.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_no_recorder_is_a_noop(tmp_path):
    """turn_latency_recorder=None (default) must not raise or break the turn."""
    llm = _ScriptedLLM([_speech_pass("你好")])
    ml = make_mindloop(memory_root=tmp_path, llm=llm)

    await ml._run_one_turn([_user_perception("hi")])  # must not raise


def _agenda_moment() -> Perception:
    return Perception(kind="AgendaMoment", t=time.time(), data={})


@pytest.mark.asyncio
async def test_suppressed_agenda_turn_records_no_first_speak(tmp_path):
    """A pure AgendaMoment turn is speech-suppressed at the ``_emit_sentence``
    chokepoint (``self._is_agenda`` guard, mind_loop.py ~L1571) — the model
    DOES produce a spoken sentence, but it never reaches the sink because the
    guard returns before the first-speak/speak_chars measurement below it.
    Regression guard: if that measurement is ever hoisted above the
    suppression guards, this test flips ``first_speak_ms`` from None to a
    real value and catches it.

    ``think_chars > 0`` proves this was a genuine full pass (measured
    earlier in ``_llm_iterate``, unaffected by the guard) — not merely an
    empty/no-op turn that trivially has no speech."""
    rec = _CapturingRecorder()
    llm = _ScriptedLLM([_speech_pass("這句話不會被送出")])
    ml = make_mindloop(memory_root=tmp_path, llm=llm, turn_latency_recorder=rec)

    await ml._run_one_turn([_agenda_moment()])

    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.first_speak_ms is None, (
        "suppressed (agenda) turn must never count as having spoken"
    )
    assert r.speak_chars == 0
    assert r.think_chars > 0, (
        "sanity: the turn ran a real pass with think content, it wasn't "
        "simply an empty no-op turn"
    )
    # Whole-branch review Finding #1: an agenda turn must be distinguishable
    # from a chat turn via perception_kinds alone — this is the whole point
    # of the field (without it, this suppressed-speech agenda turn would be
    # byte-identical in the JSONL to a real chat turn).
    assert r.perception_kinds == ["AgendaMoment"]
    assert r.perception_kinds != ["UserSpoke"]


class _StubCascadeLogger:
    """Minimal cascade_logger stub — just enough to prove `turn_id` on the
    latency record round-trips `start_turn()`'s return value."""

    FIXED_TURN_ID = "cafebabe"

    def start_turn(self) -> str:
        return self.FIXED_TURN_ID

    def log_iter(self, **kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_turn_id_present_when_cascade_logger_wired(tmp_path):
    """`turn_id` must be non-None and match the cascade_logger's minted id
    when one is wired — the precise join key back to cascade_log/trace
    JSONL (whole-branch review Finding #1)."""
    rec = _CapturingRecorder()
    llm = _ScriptedLLM([_speech_pass("你好")])
    ml = make_mindloop(
        memory_root=tmp_path, llm=llm, turn_latency_recorder=rec,
        cascade_logger=_StubCascadeLogger(),
    )

    await ml._run_one_turn([_user_perception("hi")])

    assert len(rec.records) == 1
    assert rec.records[0].turn_id == _StubCascadeLogger.FIXED_TURN_ID
