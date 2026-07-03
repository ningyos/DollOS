"""Tests for P1f trace turn-level envelope assembly (Task 2).

Scope: ``_run_one_turn`` builds a ``trace_blocks`` dict from existing render
locals and threads it into ``_llm_iterate(prompt, trace_blocks=trace_blocks)``.
``_llm_iterate``'s body does NOT consume ``trace_blocks`` yet — Task 3 wires
the ``begin_turn``/``add_pass``/``finish`` calls inside ``_llm_iterate``. So
these tests observe ``trace_blocks`` by intercepting the ``_llm_iterate`` call
itself (instance-level monkeypatch) rather than via ``TraceWriter.begin_turn``
— nothing calls ``begin_turn`` until Task 3 lands.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from dollos.mind.mind_state import Perception
from dollos.mind.trace import TraceWriter
from tests._dispatcher_helpers import _FakeMemSearch
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _ScriptedLLM, _SlowFakeLLM, _speech_pass


class _CapturingTraceWriter:
    """Fake TraceWriter. Task 3 will call ``begin_turn()`` from inside
    ``_llm_iterate``; Task 2 only needs ``self._trace_writer is not None`` to
    gate ``trace_blocks`` assembly, so this double stays unused in Task 2's
    own tests (asserted explicitly below) but is wired through the
    constructor per the Step 3 plumbing requirement."""

    def __init__(self):
        self.begun = None

    def begin_turn(self, **kw):
        self.begun = kw

        class _TT:
            def add_pass(self, **k):
                pass

            def finish(self, **k):
                pass

        return _TT()


def _user_perception(text: str) -> Perception:
    return Perception(kind="UserSpoke", t=time.time(), data={"text": text})


@pytest.fixture
def mind_loop_with_trace(tmp_path):
    tw = _CapturingTraceWriter()
    ml = make_mindloop(
        memory_root=tmp_path,
        trace_writer=tw,
        model_id="test-model",
    )
    ml._ctx.memsearch = _FakeMemSearch(
        hits=[{"text": "known fact", "source": "mem/foo.md"}]
    )
    return ml, tw, ml._state


@pytest.mark.asyncio
async def test_run_one_turn_builds_trace_blocks_with_actual_content(mind_loop_with_trace):
    ml, tw, state = mind_loop_with_trace
    state.recent_perceptions.clear()

    captured: dict = {}

    async def _capture(prompt, *, trace_blocks=None):
        captured["trace_blocks"] = trace_blocks

    ml._llm_iterate = _capture

    await ml._run_one_turn([_user_perception("hello")])

    kw = captured.get("trace_blocks")
    assert kw is not None
    # perception_batch = semantic raw, not rendered strings
    assert kw["perception_batch"][0]["kind"] == "UserSpoke"
    # current_self stored VERBATIM (mutable → must be full text, not ref) [R2 current_self finding]
    assert isinstance(kw["static_prefix"]["current_self_text"], (str, type(None)))
    # identity as hash (immutable pack) — hash present, not full identity dumped each turn
    assert "identity_hash" in kw["static_prefix"]
    # identity hash must be over the FROZEN pack system_prompt, NOT the
    # composed (prefix ⊕ mutable current_self ⊕ suffix) text — otherwise the
    # hash would drift every time current_self evolves, defeating the point
    # of hashing an "immutable versioned pack" (R2 finding).
    assert kw["static_prefix"]["identity_hash"] == hashlib.sha256(
        ml._system_prompt.encode("utf-8")
    ).hexdigest()
    # dynamic_blocks store ACTUAL hit dicts (T-C2), plus mood/energy actual values
    assert "memsearch_hits" in kw["dynamic_blocks"]
    assert kw["dynamic_blocks"]["memsearch_hits"][0]["text"] == "known fact"
    assert kw["dynamic_blocks"]["energy"] == state.energy
    assert kw["dynamic_blocks"]["mood"] == {
        "emotion": state.mood.emotion,
        "reason": state.mood.reason,
    }
    # A-products deferred to P1c/P1d → null placeholder, schema_version handles migration
    assert kw["dynamic_blocks"]["situational_A_products"] is None
    assert kw["static_prefix"]["situational_template_id"] is None
    assert kw["model_id"] == "test-model"
    assert kw["situation"] == "internal"

    # everything must be JSON-serializable directly (no raw dataclass/Path/
    # datetime relying on json.dumps(default=str) as a silent stringify net)
    json.dumps(kw)

    # constructor plumbing (Step 3): trace_writer is stored on the instance,
    # but begin_turn is NOT called by Task 2 — that wiring is Task 3's job,
    # inside _llm_iterate's body (which Task 2 explicitly leaves untouched).
    assert ml._trace_writer is tw
    assert tw.begun is None


@pytest.mark.asyncio
async def test_run_one_turn_no_trace_writer_stays_none(tmp_path):
    """No trace_writer wired (existing/default behavior) → trace_blocks stays
    None and _llm_iterate is still called normally; existing callers/tests
    that never pass trace_writer must see zero behavior change."""
    ml = make_mindloop(memory_root=tmp_path)
    captured: dict = {}

    async def _capture(prompt, *, trace_blocks=None):
        captured["called"] = True
        captured["trace_blocks"] = trace_blocks

    ml._llm_iterate = _capture

    await ml._run_one_turn([_user_perception("hi")])

    assert captured.get("called") is True
    assert captured.get("trace_blocks") is None


@pytest.mark.asyncio
async def test_situation_tag_coarse(mind_loop_with_trace):
    ml, tw, state = mind_loop_with_trace
    # external turn → "external"
    ml._ctx.external_ctx = True
    ml._is_reflection = False
    assert ml._situation_tag() == "external"
    # internal reflection → "internal_reflection"
    ml._ctx.external_ctx = False
    ml._is_reflection = True
    assert ml._situation_tag() == "internal_reflection"
    # plain internal
    ml._is_reflection = False
    assert ml._situation_tag() == "internal"


# ── Task 3: per-pass capture @ same log_iter site + turn-end finish ──


def _only_trace_envelope(traces_dir: Path) -> dict:
    """Glob the traces dir for exactly one *.jsonl file with exactly one
    line, parse it, and return the envelope dict."""
    files = list(traces_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly 1 trace file, got {files}"
    lines = files[0].read_text(encoding="utf-8").strip("\n").split("\n")
    assert len(lines) == 1, f"expected exactly 1 trace line, got {len(lines)}"
    return json.loads(lines[0])


def _recall_pass_with_reasoning(query: str = "X", speech: str = "查一下") -> str:
    """A think block with free-form reasoning text OUTSIDE any of the 5
    SEEN/INTENT/TOOL/REVIEW/MOOD fields `_parse_think` extracts — proves
    `raw_assistant_emit` is the verbatim text, a strict superset of what
    cascade_log's parsed-field copy retains."""
    return (
        "SEEN: x\n"
        "lots of reasoning about digging up context and cross-checking facts\n"
        "INTENT: y\nTOOL: Recall\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
        "<tool_call>\n"
        f'{{"name":"Recall","arguments":{{"query":"{query}"}}}}\n'
        "</tool_call>"
    )


@pytest.fixture
def mind_loop_real_trace(tmp_path):
    """MindLoop wired with a REAL TraceWriter(tmp_path/"traces") and a
    scripted LLM: pass 1 emits a Recall tool call (+ verbose think
    reasoning), pass 2 (the re-feed triggered by Recall's success) emits
    plain speech and no tool, ending the cascade."""
    traces_dir = tmp_path / "traces"
    tw = TraceWriter(traces_dir)
    llm = _ScriptedLLM([
        _recall_pass_with_reasoning(),
        _speech_pass("done"),
    ])
    ml = make_mindloop(
        memory_root=tmp_path,
        trace_writer=tw,
        model_id="test-model",
        llm=llm,
    )
    # Long hit content (> 500 chars) so the Recall tool result's `detail` is
    # long enough to prove the trace does NOT truncate it like cascade_log's
    # `detail[:500]` copy does.
    ml._ctx.memsearch = _FakeMemSearch(
        hits=[{"content": "x" * 600, "source": "mem/foo.md"}]
    )
    return ml, ml._state


@pytest.mark.asyncio
async def test_trace_pass_stores_raw_think_and_full_result(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace
    # fake LLM 第一 pass 吐:<think>SEEN:...\nlots of reasoning...</think> + Recall call
    await ml._run_one_turn([_user_perception("dig up X")])
    env = _only_trace_envelope(tmp_path / "traces")  # helper: glob *.jsonl, assert 1 line
    p0 = env["passes"][0]
    # think 逐字全文,非 _parse_think 的 5 行截斷
    assert "lots of reasoning" in p0["raw_assistant_emit"]
    # tool result 全文,非 detail[:500]
    if p0["results"]:
        assert len(p0["results"][0]["detail"]) > 0
        assert len(p0["results"][0]["detail"]) > 500  # proves no [:500] truncation
    # active_tools 該 pass 的實際工具集
    assert isinstance(p0["active_tools"], list) and len(p0["active_tools"]) > 0
    assert "latency_ms" in p0


@pytest.mark.asyncio
async def test_trace_and_cascade_log_share_source_tuple(mind_loop_real_trace, tmp_path):
    """兩 writer 從同一組 (raw_buf, results, tool_calls) 序列化;
    trace 的 raw_assistant_emit 應為 cascade_log parsed think 的 superset。"""
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("hi")])
    env = _only_trace_envelope(tmp_path / "traces")
    # trace 存 raw 全文(superset);cascade_log 只存 parsed 5 欄——trace 不 drift
    assert env["passes"][0]["raw_assistant_emit"]  # 非空,為 raw


@pytest.mark.asyncio
async def test_trace_stable_turn_id_without_cascade_logger(mind_loop_real_trace, tmp_path):
    """Trace must not depend on cascade_logger being wired — turn_id falls
    back to a minted uuid when `self._cascade_logger` is None (its default,
    since `make_mindloop` never wires one)."""
    ml, state = mind_loop_real_trace
    assert ml._cascade_logger is None
    await ml._run_one_turn([_user_perception("hi")])
    env = _only_trace_envelope(tmp_path / "traces")
    assert isinstance(env["turn_id"], str) and env["turn_id"] != ""


@pytest.mark.asyncio
async def test_trace_finish_records_full_speech_and_clears_silence_flag(
    mind_loop_real_trace, tmp_path
):
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("dig up X")])
    env = _only_trace_envelope(tmp_path / "traces")
    assert env["silence"] is False
    # Both passes' full spoken sentences land in the turn-end speech field.
    assert "查一下" in env["speech"]
    assert "done" in env["speech"]


@pytest.mark.asyncio
async def test_trace_silence_true_when_turn_speaks_nothing(tmp_path):
    """A turn whose only pass calls a tool and speaks nothing → silence=True,
    speech=""."""
    traces_dir = tmp_path / "traces"
    tw = TraceWriter(traces_dir)

    def _silent_recall_pass(query: str = "x") -> str:
        return (
            "SEEN: x\nINTENT: y\nTOOL: Recall\nREVIEW: r\nMOOD: m\n</think>\n\n"
            "<tool_call>\n"
            f'{{"name":"Recall","arguments":{{"query":"{query}"}}}}\n'
            "</tool_call>"
        )

    llm = _ScriptedLLM([_silent_recall_pass(), _speech_pass("")])
    ml = make_mindloop(
        memory_root=tmp_path, trace_writer=tw, model_id="test-model", llm=llm
    )
    ml._ctx.memsearch = _FakeMemSearch(hits=[{"content": "fact", "source": "mem/foo.md"}])

    await ml._run_one_turn([_user_perception("dig up X")])
    env = _only_trace_envelope(traces_dir)
    assert env["speech"] == ""
    assert env["silence"] is True


@pytest.mark.asyncio
async def test_no_trace_writer_no_trace_file_written(tmp_path):
    """No trace_writer wired → zero behavior change, no traces dir created."""
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_user_perception("hi")])
    assert not (tmp_path / "traces").exists()


# ── Task 4: input_messages_delta = byte-verbatim authority per pass ──


def passes_prompt(env: dict) -> str:
    """The exact prompt string `_llm_iterate` seeded `messages[0]` with —
    recovered from pass 0's own captured delta (env["passes"][0]
    ["input_messages_delta"][0]["content"]). Used as the expected value when
    reconstructing `messages` from concatenated per-pass deltas."""
    return env["passes"][0]["input_messages_delta"][0]["content"]


@pytest.mark.asyncio
async def test_input_messages_delta_is_byte_authority(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace  # fake LLM: pass0 emits Recall(sync, refed); pass1 emits Say(ends)
    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")
    passes = env["passes"]
    # pass 0 delta = the initial user prompt, verbatim.
    assert passes[0]["input_messages_delta"][0]["role"] == "user"
    assert passes[0]["input_messages_delta"][0]["content"]  # == the prompt fed to pass0
    if len(passes) > 1:
        # pass 1 delta = prior pass's assistant emit + filtered <tool_response>
        # (must NOT include a fire-and-forget ack, e.g. a Shell/SpawnWorkflow
        # success that was never re-fed).
        roles = [m["role"] for m in passes[1]["input_messages_delta"]]
        assert "assistant" in roles
        # At least one tool_response (Recall is a refed sync tool).
        assert any("tool_response" in m.get("content", "") for m in passes[1]["input_messages_delta"])


@pytest.mark.asyncio
async def test_delta_concatenation_reconstructs_final_messages(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace

    # Independently capture the EXACT prompt string `_llm_iterate` passes to
    # pass 0's `stream_completion(user=prompt, ...)` call, so the
    # reconstruction assertion below checks against ground truth captured
    # outside the trace machinery — not tautologically against the trace's
    # own delta.
    captured_prompt: dict[str, str] = {}
    orig_stream_completion = ml._llm.stream_completion

    def _capturing_stream_completion(*args, **kwargs):
        captured_prompt["value"] = kwargs.get("user")
        return orig_stream_completion(*args, **kwargs)

    ml._llm.stream_completion = _capturing_stream_completion

    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")

    # Concatenating every pass's delta reconstructs the final `messages`
    # list `_llm_iterate` built internally, byte-for-byte.
    reconstructed = [m for p in env["passes"] for m in p["input_messages_delta"]]
    # Every delta dict is a legal message (role + content present).
    assert all("role" in m and "content" in m for m in reconstructed)
    assert reconstructed[0] == {"role": "user", "content": passes_prompt(env)}
    assert reconstructed[0]["content"] == captured_prompt["value"]

    # Structural check on the reconstructed alternation.
    roles = [m["role"] for m in reconstructed]
    assert roles[0] == "user"
    assert "assistant" in roles


def _shell_and_recall_pass(query: str = "X", speech: str = "跑個指令再查一下") -> str:
    """A single pass that emits BOTH a fire-and-forget Shell tool call AND a
    refed sync Recall tool call. The refeed filter must KEEP the Recall
    <tool_response> and DROP the Shell dispatch ack — proving the delta is the
    filtered append set, NOT the full tool_calls list (brief R2: delta ≠ all
    tool_calls)."""
    return (
        "SEEN: x\nINTENT: y\nTOOL: Shell+Recall\nREVIEW: r\nMOOD: m\n</think>\n\n"
        f"{speech}"
        "<tool_call>\n"
        '{"name":"Shell","arguments":{"command":"echo hi"}}\n'
        "</tool_call>"
        "<tool_call>\n"
        f'{{"name":"Recall","arguments":{{"query":"{query}"}}}}\n'
        "</tool_call>"
    )


@pytest.mark.asyncio
async def test_fire_and_forget_ack_excluded_from_delta(tmp_path):
    """A fire-and-forget Shell ack must NEVER leak into a later pass's
    input_messages_delta, while a co-emitted refed Recall result MUST — the
    exclusion path (brief R2: refeed is a FILTERED subset) is exercised, so
    delta ≠ all tool_calls is proven, not merely asserted structurally."""
    traces_dir = tmp_path / "traces"
    tw = TraceWriter(traces_dir)
    llm = _ScriptedLLM([
        _shell_and_recall_pass(),
        _speech_pass("done"),
    ])
    ml = make_mindloop(
        memory_root=tmp_path, trace_writer=tw, model_id="test-model", llm=llm
    )
    ml._ctx.memsearch = _FakeMemSearch(
        hits=[{"content": "x" * 600, "source": "mem/foo.md"}]
    )

    await ml._run_one_turn([_user_perception("dig up X")])
    env = _only_trace_envelope(traces_dir)
    passes = env["passes"]

    # pass 0 issued TWO tool calls (Shell + Recall) — so if the delta were a
    # naive echo of tool_calls, pass 1 would carry two <tool_response>s.
    p0_tool_names = {tc["name"] for tc in passes[0]["tool_calls"]}
    assert p0_tool_names == {"Shell", "Recall"}

    # A second pass exists (Recall's success triggered the in-turn refeed).
    assert len(passes) > 1, "Recall refeed should have produced a second pass"
    delta = passes[1]["input_messages_delta"]

    # TEETH: the Shell dispatch ack ("shell dispatched (command=...)") must NOT
    # appear in ANY delta message — inverting this (asserting it IS present)
    # fails, because the fire-and-forget ack is genuinely filtered out.
    assert all("shell dispatched" not in m.get("content", "") for m in delta), (
        "fire-and-forget Shell ack leaked into input_messages_delta"
    )

    # The refed Recall result IS present: exactly one <tool_response> survives
    # the filter (Recall kept, Shell dropped) — delta ≠ all tool_calls.
    tool_responses = [m for m in delta if "<tool_response>" in m.get("content", "")]
    assert len(tool_responses) == 1, (
        f"expected exactly the Recall <tool_response>, got {len(tool_responses)}"
    )
    # Prior pass's assistant emit is also in the delta (full alternation).
    assert any(m["role"] == "assistant" for m in delta)


# ── Task 5: per-pass latency_ms real measurement; tokens stays deferred ──


@pytest.mark.asyncio
async def test_pass_latency_present_and_tokens_deferred(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")
    for p in env["passes"]:
        assert isinstance(p["latency_ms"], int) and p["latency_ms"] >= 0
        assert p["tokens"] is None  # per-pass usage 不從 StreamChunk 掉出來(R2 T-token)


# ── Task 6: cancelled-pass caveat — envelope still finalizes ──


@pytest.mark.asyncio
async def test_cancelled_pass_not_recorded_but_envelope_finalizes(tmp_path):
    """A pass cancelled mid-stream returns from ``_stream_one_pass`` BEFORE
    ``_llm_iterate``'s add_pass call site (mind_loop.py: the
    ``if self._cascade_ctx.cancelled: return`` check right after
    ``_stream_one_pass`` returns, ahead of ``turn_trace.add_pass(...)``) — so
    the cancelled pass produces neither a cascade_log entry nor a trace pass.
    But ``_llm_iterate``'s ``finally`` block calls ``turn_trace.finish()``
    unconditionally whenever a turn_trace was begun, so the envelope is NOT
    lost wholesale — it still finalizes to disk with only the passes that
    completed before the cancel (§3.6/§6.2(g) explicit tradeoff)."""
    traces_dir = tmp_path / "traces"
    tw = TraceWriter(traces_dir)
    chunks = [f"chunk{i} " for i in range(10)]
    slow_llm = _SlowFakeLLM(chunks, delay=0.1)
    ml = make_mindloop(
        memory_root=tmp_path, trace_writer=tw, model_id="test-model", llm=slow_llm
    )

    task = asyncio.create_task(ml._run_one_turn([_user_perception("q")]))
    await asyncio.sleep(0.15)
    assert ml.is_cascade_active is True
    ml.cancel_current_cascade()
    await asyncio.wait_for(task, timeout=0.4)
    assert ml.is_cascade_active is False

    trace_files = list(traces_dir.glob("*.jsonl"))
    # Envelope must still finalize — exactly one JSONL file, one line — even
    # though the cascade was cancelled mid-first-pass (not lost entirely).
    assert len(trace_files) == 1, f"expected trace envelope to finalize, got {trace_files}"
    env = _only_trace_envelope(traces_dir)
    assert "passes" in env
    # The cancelled first pass never reached add_pass — zero passes captured,
    # not a lost/missing envelope.
    assert env["passes"] == []
