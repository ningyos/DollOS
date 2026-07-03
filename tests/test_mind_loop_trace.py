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

import hashlib
import json
import time

import pytest

from dollos.mind.mind_state import Perception
from tests._dispatcher_helpers import _FakeMemSearch
from tests._mindloop_factory import make_mindloop


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
