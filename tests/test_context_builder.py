"""ContextBuilder — append-only conversations per place (plan 2026-10-18 Task 9).

Every MindLoop request of a conversation must be a byte prefix of the next
one — across passes and across turn kinds — so the server's cache resumes;
a new page opens only on the four listed conditions. Also covers interrupts
and errors, the diary one-off, isolation and the LRU, the body/cognition
blocks, the token accounting and the trace.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import date, datetime

import pytest

from dollos.cascade.tool_loop import NOT_RUN
from dollos.llm.model import ModelError, SpeechDelta, ThinkDelta, ToolCall, ToolCalls
from dollos.mind import self_history
from dollos.mind.context_builder import PRIVATE
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.trace import TraceWriter
from dollos.perception.cognition import CognitionWorker
from dollos.perception.system_pulse import PulseSample, SystemPulse
from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop


def _user(text: str) -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


def _stranger(text: str, channel: str = "c1") -> Perception:
    return Perception(kind="ChannelMessage", t=time.time(), data={
        "content": text, "channel_id": channel, "channel": "general",
        "author": "rando", "author_is_owner": False, "is_dm": False,
    })


def _calls(*specs: tuple[str, dict]) -> ToolCalls:
    return ToolCalls(tuple(
        ToolCall(id=f"call_{name}_{i}", name=name, arguments=json.dumps(args))
        for i, (name, args) in enumerate(specs)
    ))


def _loop(tmp_path, model, **kw):
    queue = PerceptionQueue(wal=None)
    ml = make_mindloop(memory_root=tmp_path, queue=queue, llm=model, **kw)
    return ml, queue


async def _turn(ml, queue, *percs: Perception) -> None:
    for p in percs:
        queue.put(p)
    await ml.iterate()


def _assert_prefix_chain(calls: list[dict]) -> None:
    """Each request is a byte prefix of the next, with the same tools."""
    for a, b in zip(calls, calls[1:], strict=False):
        assert json.dumps(b["messages"][: len(a["messages"])]) == json.dumps(a["messages"])
        assert json.dumps(b["tools"]) == json.dumps(a["tools"])


def _users(call: dict) -> list[str]:
    return [m["content"] for m in call["messages"] if m["role"] == "user"]


# ---------------------------------------------------------------------------
# Prefix: across passes and across turn kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_request_is_a_prefix_of_the_next_across_turn_kinds(tmp_path):
    """owner (two passes) → agenda → reflection (two passes) → owner: one
    conversation, every request the byte prefix of the next."""
    model = FakeModel([
        [ThinkDelta("look it up"), SpeechDelta("Let me check. "),
         _calls(("Recall", {"query": "coffee"}))],
        [SpeechDelta("Found it.")],
        [ThinkDelta("my own goals")],
        [_calls(("NoteMemory", {"text": "noticed I like tides"}))],
        [],
        [SpeechDelta("Hi again.")],
    ])
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("do I like coffee?"))
    await _turn(ml, queue, Perception(kind="AgendaMoment", t=time.time(), data={}))
    await _turn(ml, queue, Perception(
        kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 30},
    ))
    await _turn(ml, queue, _user("hello"))

    assert model.pass_count == 6
    _assert_prefix_chain(model.calls)
    assert {c["conversation"] for c in model.calls} == {(PRIVATE, 1)}
    # The assistant message went back verbatim, reasoning included.
    second = model.calls[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "look it up"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"query": "coffee"}'
    # One new user message per turn, the time first.
    assert len(_users(model.calls[-1])) == 1 + 4  # snapshot + 4 turns
    assert all(u.startswith("[") for u in _users(model.calls[-1]))


@pytest.mark.asyncio
async def test_page_opens_with_system_then_snapshot_then_delta(tmp_path):
    model = FakeModel([[SpeechDelta("hey")]])
    ml, queue = _loop(tmp_path, model, primary_language="English")
    await _turn(ml, queue, _user("first words"))

    system, snapshot, delta = model.calls[0]["messages"]
    assert system["role"] == "system"
    assert "[Memory guideline]" in system["content"] and "English" in system["content"]
    assert snapshot["content"].startswith("[New conversation page")
    assert "[Memory guideline]" not in snapshot["content"] + delta["content"]
    # The snapshot does not repeat this turn's own perception; the delta has it.
    assert "first words" not in snapshot["content"]
    assert "UserSpoke: 'first words'" in delta["content"]


# ---------------------------------------------------------------------------
# New page: the four conditions, and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_page_when_last_call_reached_the_budget(tmp_path):
    model = FakeModel(
        [[SpeechDelta("a")], [SpeechDelta("b")], [SpeechDelta("c")]],
        usages=[(900, 50), (990, 20), (400, 10)],
    )
    ml, queue = _loop(tmp_path, model, conversation_budget_tokens=1000)
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))    # 950 < 1000: same page
    await _turn(ml, queue, _user("three"))  # 1010 >= 1000: new page

    assert [c["conversation"] for c in model.calls] == [
        (PRIVATE, 1), (PRIVATE, 1), (PRIVATE, 2),
    ]
    assert [c["prior_tokens"] for c in model.calls] == [0, 950, 0]
    _assert_prefix_chain(model.calls[:2])
    third = model.calls[2]["messages"]
    assert len(third) == 3 and third[1]["content"].startswith("[New conversation page")
    assert "'three'" in third[2]["content"]


@pytest.mark.asyncio
async def test_new_page_when_current_self_changes(tmp_path):
    model = FakeModel([[SpeechDelta("a")], [SpeechDelta("b")]])
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("one"))
    self_history.log_event(
        tmp_path / "self_history.jsonl", kind="evo_adopt", text="新版的我" + "字" * 90,
    )
    await _turn(ml, queue, _user("two"))

    assert [c["conversation"] for c in model.calls] == [(PRIVATE, 1), (PRIVATE, 2)]
    assert "新版的我" in model.calls[1]["messages"][0]["content"]
    assert model.calls[1]["messages"][1]["content"].startswith("[New conversation page")


@pytest.mark.asyncio
async def test_new_page_after_daemon_restart(tmp_path):
    state = MindState()
    first = FakeModel([[SpeechDelta("a")]])
    ml, queue = _loop(tmp_path, first, state=state)
    await _turn(ml, queue, _user("before restart"))

    second = FakeModel([[SpeechDelta("b")]])
    ml2, queue2 = _loop(tmp_path, second, state=state)
    await _turn(ml2, queue2, _user("after restart"))

    system, snapshot, delta = second.calls[0]["messages"]
    assert second.calls[0]["conversation"] == (PRIVATE, 1)
    assert snapshot["content"].startswith("[New conversation page")
    # The recap has what came before (state survives), not this turn's input.
    assert "before restart" in snapshot["content"]
    assert "after restart" not in snapshot["content"]
    assert "after restart" in delta["content"]


@pytest.mark.asyncio
async def test_new_page_after_server_error(tmp_path):
    model = FakeModel([ModelError("server", "prompt too long"), [SpeechDelta("ok")]])
    ml, queue = _loop(tmp_path, model)
    with pytest.raises(ModelError):
        await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    assert [c["conversation"] for c in model.calls] == [(PRIVATE, 1), (PRIVATE, 2)]
    assert len(model.calls[1]["messages"]) == 3
    assert "'one'" not in model.calls[1]["messages"][2]["content"]


@pytest.mark.asyncio
async def test_turn_kind_safe_mode_and_idle_never_open_a_page(tmp_path):
    model = FakeModel([[SpeechDelta("a")], [], [], [SpeechDelta("b")]])
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, Perception(kind="AgendaMoment", t=time.time(), data={}))
    ml._state.safe_mode, ml._state.safe_mode_reason = True, "3 consecutive tool failures"
    await _turn(ml, queue, Perception(kind="PulseMoment", t=time.time(), data={"detail": "hot"}))
    ml._state.last_iter_at -= 86_400  # a day idle
    await _turn(ml, queue, _user("back"))

    assert {c["conversation"] for c in model.calls} == {(PRIVATE, 1)}
    _assert_prefix_chain(model.calls)


# ---------------------------------------------------------------------------
# Errors and interrupts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_keeps_the_user_message_and_appends_the_next(tmp_path):
    model = FakeModel([ModelError("transport", "unreachable"), [SpeechDelta("ok")]])
    ml, queue = _loop(tmp_path, model)
    with pytest.raises(ModelError):
        await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    _assert_prefix_chain(model.calls)
    msgs = model.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "user", "user"]
    assert "one" in msgs[2]["content"] and "two" in msgs[3]["content"]
    assert model.calls[1]["conversation"] == (PRIVATE, 1)


@pytest.mark.asyncio
async def test_transport_error_after_output_keeps_what_was_said(tmp_path):
    model = FakeModel([
        [ThinkDelta("hmm"), SpeechDelta("First sentence. "), SpeechDelta("half a"),
         ModelError("transport", "stream cut")],
        [SpeechDelta("ok")],
    ])
    ml, queue = _loop(tmp_path, model)
    with pytest.raises(ModelError):
        await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    _assert_prefix_chain(model.calls)
    msgs = model.calls[1]["messages"]
    assert msgs[3] == {
        "role": "assistant", "content": "First sentence. ", "reasoning_content": "hmm",
    }
    assert msgs[4]["role"] == "user" and "two" in msgs[4]["content"]


class _CancelAfter(FakeModel):
    """Cancels the loop's turn after ``after`` events of call ``on_call``."""

    def __init__(self, replies, *, on_call: int, after: int):
        super().__init__(replies)
        self.loop = None
        self._on_call, self._after = on_call, after

    async def stream(self, **kw):
        idx = len(self.calls)
        n = 0
        async for ev in super().stream(**kw):
            if idx == self._on_call and n == self._after:
                self.loop.cancel_current_cascade()
            yield ev
            n += 1
        if idx == self._on_call and n == self._after:
            self.loop.cancel_current_cascade()


@pytest.mark.asyncio
async def test_interrupt_mid_stream_keeps_the_partial_reply(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    model = _CancelAfter(
        [[ThinkDelta("hmm"), SpeechDelta("First sentence. "), SpeechDelta("not yet said"),
          SpeechDelta(" more.")],
         [SpeechDelta("ok")]],
        on_call=0, after=3,
    )
    ml, queue = _loop(tmp_path, model, sink=sink)
    model.loop = ml
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    _assert_prefix_chain(model.calls)
    msgs = model.calls[1]["messages"]
    # The record matches what was actually said: the buffered half sentence
    # never reached the sink, so it is not in the conversation either.
    assert msgs[3] == {
        "role": "assistant", "content": "First sentence. ", "reasoning_content": "hmm",
    }


@pytest.mark.asyncio
async def test_interrupt_before_any_output_adds_nothing(tmp_path):
    model = _CancelAfter([[SpeechDelta("never")], [SpeechDelta("ok")]], on_call=0, after=0)
    ml, queue = _loop(tmp_path, model)
    model.loop = ml
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    _assert_prefix_chain(model.calls)
    assert [m["role"] for m in model.calls[1]["messages"]] == ["system", "user", "user", "user"]


@pytest.mark.asyncio
async def test_interrupt_after_tool_calls_answers_every_call(tmp_path):
    """Interrupted while the reply's tools run: the run one keeps its result,
    the rest answer "not run", so every call has its tool message."""
    model = FakeModel([
        [SpeechDelta("Noting both. "),
         _calls(("NoteMemory", {"text": "first"}), ("NoteMemory", {"text": "second"}))],
        [SpeechDelta("ok")],
    ])
    ml, queue = _loop(tmp_path, model)
    dispatch = ml._dispatch_tool

    async def _dispatch_then_interrupt(name, args):
        r = await dispatch(name, args)
        ml.cancel_current_cascade()
        return r

    ml._dispatch_tool = _dispatch_then_interrupt
    await _turn(ml, queue, _user("note two things"))
    ml._dispatch_tool = dispatch
    await _turn(ml, queue, _user("two"))

    assert model.pass_count == 2  # the interrupted turn ran no second pass
    _assert_prefix_chain(model.calls)
    msgs = model.calls[1]["messages"]
    assistant, run, not_run, nxt = msgs[3:7]
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_NoteMemory_0", "call_NoteMemory_1"]
    assert run["tool_call_id"] == "call_NoteMemory_0" and run["content"] != NOT_RUN
    assert not_run == {"role": "tool", "tool_call_id": "call_NoteMemory_1", "content": NOT_RUN}
    assert nxt["role"] == "user"


@pytest.mark.asyncio
async def test_interrupt_right_after_the_stream_runs_no_tool(tmp_path):
    model = _CancelAfter(
        [[SpeechDelta("Noting. "), _calls(("NoteMemory", {"text": "x"}))],
         [SpeechDelta("ok")]],
        on_call=0, after=3,  # after the End event
    )
    ml, queue = _loop(tmp_path, model)
    model.loop = ml
    ran: list[str] = []
    dispatch = ml._dispatch_tool

    async def _spy(name, args):
        ran.append(name)
        return await dispatch(name, args)

    ml._dispatch_tool = _spy
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _user("two"))

    _assert_prefix_chain(model.calls)
    assert ran == []
    msgs = model.calls[1]["messages"]
    assert msgs[4] == {"role": "tool", "tool_call_id": "call_NoteMemory_0", "content": NOT_RUN}


# ---------------------------------------------------------------------------
# Diary: a one-off request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diary_turn_is_a_one_off_request(tmp_path):
    pulse = _FakeBlocks("Self pulse")
    model = FakeModel([
        [SpeechDelta("morning")],
        [_calls(("WriteDiary", {"content": "今天很平靜。"}))],
        [],
        [SpeechDelta("evening")],
    ])
    ml, queue = _loop(tmp_path, model, system_pulse=pulse, self_profile_enabled=True)
    (tmp_path / "self_profile.md").write_text(
        "## 我學到的自己\n- [s1·2026-10-01] likes the tide\n", encoding="utf-8",
    )
    await _turn(ml, queue, _user("good morning"))
    private_before = json.dumps(ml._contexts.get(PRIVATE).messages)
    log = tmp_path / "transcripts" / f"{date.today():%Y-%m-%d}.md"
    with log.open("a", encoding="utf-8") as f:  # the owner turn already wrote to it
        f.write("- 10:00 went for a walk\n")
    pulse.version += 1  # a body change the diary must not consume
    await _turn(ml, queue, Perception(kind="DiaryMoment", t=time.time(), data={}))

    diary = model.calls[1]
    system, request = diary["messages"]
    assert system == model.calls[0]["messages"][0]
    assert diary["tools"] == model.calls[0]["tools"]  # internal list: prefix resumes
    assert diary["conversation"] is None
    assert request["content"].startswith("[New conversation page")
    assert "[Self profile]" in request["content"] and "likes the tide" in request["content"]
    assert "[Today's log]" in request["content"] and "went for a walk" in request["content"]
    assert "DiaryMoment:" in request["content"]
    assert "[Self pulse]" not in request["content"]
    # The diary's own passes grow on its own list; private is untouched.
    assert model.calls[2]["messages"][:2] == diary["messages"]
    assert json.dumps(ml._contexts.get(PRIVATE).messages) == private_before
    # The next private turn picks up the body change the diary left alone.
    await _turn(ml, queue, _user("evening"))
    assert model.calls[3]["conversation"] == (PRIVATE, 1)
    _assert_prefix_chain([model.calls[0], model.calls[3]])
    assert f"[Self pulse] v{pulse.version}" in model.calls[3]["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# Isolation, LRU
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_and_channel_conversations_do_not_mix(tmp_path):
    model = FakeModel([[SpeechDelta("hi owner")], [SpeechDelta("hi rando")],
                       [SpeechDelta("hi owner again")], [SpeechDelta("bye rando")]])
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("owner-secret-xyz"))
    await _turn(ml, queue, _stranger("stranger-hello-abc"))
    await _turn(ml, queue, _user("owner-again"))
    await _turn(ml, queue, _stranger("stranger-bye"))

    private = [c for c in model.calls if c["conversation"][0] == PRIVATE]
    channel = [c for c in model.calls if c["conversation"][0] == "ch:c1"]
    assert len(private) == 2 and len(channel) == 2
    _assert_prefix_chain(private)
    _assert_prefix_chain(channel)
    for c in channel:
        dumped = json.dumps(c["messages"], ensure_ascii=False)
        assert "owner-secret-xyz" not in dumped and "owner-again" not in dumped
    for c in private:
        dumped = json.dumps(c["messages"], ensure_ascii=False)
        assert "stranger-hello-abc" not in dumped and "stranger-bye" not in dumped
    assert private[0]["tools"] != channel[0]["tools"]


@pytest.mark.asyncio
async def test_lru_drops_the_least_recently_used_channel(tmp_path):
    model = FakeModel()
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("private first"))
    for n in range(1, 9):
        await _turn(ml, queue, _stranger(f"hi {n}", channel=f"c{n}"))
    await _turn(ml, queue, _stranger("c1 again", channel="c1"))  # c1 is now fresh
    await _turn(ml, queue, _stranger("hi 9", channel="c9"))      # 9th: c2 goes

    assert ml._contexts.get("ch:c2") is None
    assert ml._contexts.get("ch:c1") is not None
    assert ml._contexts.get(PRIVATE) is not None  # never dropped
    before = len(model.calls)
    await _turn(ml, queue, _stranger("c2 back", channel="c2"))
    reopened = model.calls[before]["messages"]
    assert len(reopened) == 3 and reopened[1]["content"].startswith("[New conversation page")
    assert "hi 2" not in reopened[2]["content"]


# ---------------------------------------------------------------------------
# Body and cognition blocks: private only, "changed" never eaten elsewhere
# ---------------------------------------------------------------------------


class _FakeBlocks:
    """A SystemPulse / CognitionWorker stand-in: ``snapshot()`` gives the
    block once per version, ``current_block()`` always (marking it sent)."""

    def __init__(self, name: str):
        self.name, self.version, self.sent = name, 0, None
        self.snapshot_calls = 0

    def _block(self) -> str:
        return f"[{self.name}] v{self.version}"

    def snapshot(self):
        self.snapshot_calls += 1
        if self.sent == self.version:
            return None
        self.sent = self.version
        return self._block()

    def current_block(self):
        self.sent = self.version
        return self._block()

    def latest_sample(self):
        return None


@pytest.mark.asyncio
async def test_body_and_cognition_only_on_private_and_not_eaten_by_channels(tmp_path):
    pulse, cognition = _FakeBlocks("Self pulse"), _FakeBlocks("Cognition")
    model = FakeModel()
    ml, queue = _loop(tmp_path, model, system_pulse=pulse, cognition=cognition)
    await _turn(ml, queue, _user("hi"))
    snapshot = model.calls[0]["messages"][1]["content"]
    assert "[Self pulse] v0" in snapshot and "[Cognition] v0" in snapshot
    assert "[Self pulse]" not in model.calls[0]["messages"][2]["content"]

    pulse.version, cognition.version = 1, 1
    calls_before = (pulse.snapshot_calls, cognition.snapshot_calls)
    await _turn(ml, queue, _stranger("hello"))
    assert (pulse.snapshot_calls, cognition.snapshot_calls) == calls_before
    channel = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    assert "[Self pulse]" not in channel and "[Cognition]" not in channel

    await _turn(ml, queue, _user("still here"))
    delta = model.calls[2]["messages"][-1]["content"]
    assert "[Self pulse] v1" in delta and "[Cognition] v1" in delta


def test_system_pulse_current_block_renders_and_marks_sent():
    pulse = SystemPulse(include_active_window=False)
    assert pulse.current_block() is None  # no sample yet
    pulse._last_sample = PulseSample(taken_at=datetime.now(), load1=0.5, ncpu=8)
    assert pulse.snapshot() is not None
    assert pulse.snapshot() is None
    block = pulse.current_block()  # unchanged, still rendered for a new page
    assert block is not None and block.startswith("[Self pulse")
    assert pulse.snapshot() is None  # and counted as sent


@pytest.mark.asyncio
async def test_cognition_current_block_renders_and_marks_sent(tmp_path):
    rec = TelemetryRecorder(tmp_path)
    await rec.record(LLMCallRecord(ts=time.time(), prompt_tokens=100, completion_tokens=10,
                                   latency_total_ms=1000.0, new_prompt_tokens=100))
    worker = CognitionWorker(recorder=rec)
    assert worker.snapshot() is not None
    assert worker.snapshot() is None
    assert worker.current_block().startswith("[Cognition")
    assert worker.snapshot() is None


# ---------------------------------------------------------------------------
# Token accounting: only what each call appended
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_charges_only_new_prompt_tokens(tmp_path):
    model = FakeModel(
        [[SpeechDelta("a")], [SpeechDelta("b")]], usages=[(1000, 100), (1300, 50)],
    )
    state = MindState()
    ml, queue = _loop(tmp_path, model, state=state, energy_enabled=True,
                      token_per_energy_unit=2000.0)
    await _turn(ml, queue, _user("one"))
    assert state.energy == pytest.approx(1.0 - (100 + 0.25 * 1000) / 2000)
    await _turn(ml, queue, _user("two"))
    assert model.calls[1]["prior_tokens"] == 1100
    # 1300 - 1100 = 200 new prompt tokens, not the whole 1300.
    assert ml._turn_energy_cost == pytest.approx((50 + 0.25 * 200) / 2000)


@pytest.mark.asyncio
async def test_daily_quota_sums_new_prompt_tokens(tmp_path):
    rec = TelemetryRecorder(tmp_path)
    now = time.time()
    # An old record from before the upgrade (no new_prompt_tokens) counts whole.
    await rec.record(LLMCallRecord(ts=now, prompt_tokens=500, completion_tokens=20))
    await rec.record(LLMCallRecord(ts=now, prompt_tokens=3000, completion_tokens=30,
                                   new_prompt_tokens=200))
    snap, _ = CognitionWorker(recorder=rec)._compute()
    assert snap.tokens_used_today == 500 + 20 + 200 + 30
    assert snap.last_prompt_tokens == 3000  # mind room: the conversation's length


@pytest.mark.asyncio
async def test_prior_tokens_survives_a_call_without_usage(tmp_path):
    model = FakeModel(
        [[SpeechDelta("a")], ModelError("transport", "down"), [SpeechDelta("c")]],
        usages=[(800, 40)],
    )
    ml, queue = _loop(tmp_path, model)
    await _turn(ml, queue, _user("one"))
    with pytest.raises(ModelError):
        await _turn(ml, queue, _user("two"))
    await _turn(ml, queue, _user("three"))
    assert [c["prior_tokens"] for c in model.calls] == [0, 840, 840]


# ---------------------------------------------------------------------------
# Telemetry and trace
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.records = []

    async def record(self, rec):
        self.records.append(rec)


@pytest.mark.asyncio
async def test_turn_latency_records_conversation_and_epoch(tmp_path):
    rec = _Recorder()
    model = FakeModel([[SpeechDelta("a")], [SpeechDelta("b")]])
    ml, queue = _loop(tmp_path, model, turn_latency_recorder=rec)
    await _turn(ml, queue, _user("one"))
    await _turn(ml, queue, _stranger("two"))
    assert [(r.conversation, r.epoch) for r in rec.records] == [(PRIVATE, 1), ("ch:c1", 1)]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_trace_rebuilds_the_messages_actually_sent(tmp_path):
    """epoch_start (tools hash, system hash, snapshot) plus each pass's
    input_messages_delta rebuild every request — including the partial
    assistant message an interrupted turn left behind (its own pass is not
    traced) and a page that opened mid-run."""
    traces = tmp_path / "traces"
    model = _CancelAfter(
        [
            [SpeechDelta("Checking. "), _calls(("Recall", {"query": "tea"}))],  # 0
            [SpeechDelta("Tea it is.")],                                       # 1
            [ThinkDelta("hm"), SpeechDelta("Cut off here. "), SpeechDelta("x")],  # 2 (interrupted)
            [SpeechDelta("Back.")],                                            # 3
            [SpeechDelta("New page.")],                                        # 4
        ],
        on_call=2, after=2,
    )
    ml, queue = _loop(
        tmp_path, model, trace_writer=TraceWriter(traces), conversation_budget_tokens=10_000,
    )
    model.loop = ml
    model._usages = [(100, 10), (300, 10), (None, None), (500, 9_600), (200, 5)]
    for text in ("tea?", "and?", "still there?", "new page please"):
        await _turn(ml, queue, _user(text))

    envelopes = [
        json.loads(line)
        for f in sorted(traces.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    rebuilt: dict[tuple, list] = {}
    starts: dict[tuple, tuple] = {}
    sent: list[tuple[tuple, list]] = []
    for env in envelopes:
        key = (env["conversation"], env["epoch"])
        if env["epoch_start"] is not None:
            start = env["epoch_start"]
            rebuilt[key] = [{"role": "user", "content": start["snapshot"]}]
            starts[key] = (start["tools_hash"], start["system_hash"])
        for p in env["passes"]:
            lo, hi = p["msg_range"]
            assert lo == len(rebuilt[key]) + 1  # +1: the system message
            rebuilt[key].extend(p["input_messages_delta"])
            assert hi == len(rebuilt[key]) + 1
            sent.append((starts[key], list(rebuilt[key])))

    traced_calls = [c for i, c in enumerate(model.calls) if i != 2]
    assert len(sent) == len(traced_calls) == 4
    for (hashes, msgs), call in zip(sent, traced_calls, strict=True):
        assert msgs == call["messages"][1:]
        assert hashes == (_sha(json.dumps(call["tools"], ensure_ascii=False)),
                          _sha(call["messages"][0]["content"]))
    # The interrupted turn's partial reply is in the next traced pass.
    assert {"role": "assistant", "content": "Cut off here. ", "reasoning_content": "hm"} in \
        envelopes[2]["passes"][0]["input_messages_delta"]
    assert [env["epoch"] for env in envelopes] == [1, 1, 1, 2]
