"""Token-driven energy drain — the metabolic model's behavioral core (spec
2026-07-10 §2.2, Task 2). Task 1 threaded a per-call ``on_usage`` callback
through the LLM stream stack (Provider -> LLMAdapter -> ComposedLLMAdapter);
this task makes MindLoop its own consumer: ``_on_turn_usage`` accumulates
prompt/completion tokens across every cascade pass of ONE turn (a turn can
re-feed through ``stream_messages`` multiple times — see
tests/test_mind_loop.py::test_recall_result_refed_in_same_turn for the
re-feed mechanics), then the SAME drain site (mind_loop.py's
``_run_one_turn`` tail, gated by ``produced and consumes`` — kept verbatim
from the flat cost_per_turn model) computes:

- measured: ``(completion + 0.25*prompt) / token_per_energy_unit`` whenever
  at least ONE pass this turn reported usage.
- flat_legacy (D1, the ONE sanctioned no-fallback degrade): plain
  ``cost_per_turn`` when NO pass reported usage all turn (both prompt and
  completion stayed None). Every OTHER existing energy test in the suite
  (test_energy.py, test_energy_origin.py, test_external_safety.py,
  test_mind_loop_agenda_turn.py) drives a fake LLM that never invokes
  ``on_usage`` at all — so those turns already route through flat_legacy,
  and their drain amounts (still exactly ``cost_per_turn``) needed NO
  assertion changes from this task.

v1a: the thermal multiplier is fixed at 1.0 (Task 5 wires the real one) —
drain == token_cost this task, so the arithmetic below has no multiplier
term.
"""
from __future__ import annotations

import pytest

from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM, _ScriptedLLM, _recall_pass, _speech_pass


class _UsageLLM:
    """Single-pass fake that reports (prompt_tokens, completion_tokens) via
    ``on_usage`` exactly once — mirroring one real transport call (Task 1's
    ``on_usage(prompt_tokens, completion_tokens)`` in transport.py's
    ``finally`` block). Converges after pass 1 (``TOOL: none`` — never
    triggers a ``stream_messages`` re-feed)."""

    def __init__(self, speech: str, prompt: int | None, completion: int | None):
        self._speech = speech
        self._prompt = prompt
        self._completion = completion

    async def stream_completion(
        self, system, user, prefill, max_tokens=1024, grammar=None,
        purpose="cascade", on_usage=None,
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        if on_usage is not None:
            on_usage(self._prompt, self._completion)
        yield _Chunk(
            text=f"SEEN: x\nTOOL: none\n</think>\n\n{self._speech}", done=True
        )

    async def stream_messages(
        self, system, messages, max_tokens=1024, grammar=None,
        purpose="cascade", stop=None, tools=None, on_usage=None,
    ):
        class _Chunk:
            def __init__(self, text, done):
                self.text = text
                self.done = done

        yield _Chunk(text="TOOL: none\n</think>\n\n", done=True)


def _user_turn_queue() -> PerceptionQueue:
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    return queue


@pytest.mark.asyncio
async def test_measured_drain_uses_token_formula(tmp_path):
    """(completion + 0.25*prompt) / token_per_energy_unit, tagged measured."""
    state = MindState()
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=_user_turn_queue(),
        llm=_UsageLLM("hello", prompt=400, completion=1000),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    assert state.energy == 1.0
    await ml.iterate()

    # (1000 + 0.25*400) / 2000 = 1100/2000 = 0.55
    assert state.energy == pytest.approx(1.0 - 0.55)
    assert ml._turn_cost_mode == "measured"
    assert ml._turn_tokens_total == 1400
    assert ml._turn_energy_cost == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_heavier_turn_drains_more_than_lighter_turn(tmp_path):
    """More tokens this turn -> strictly more energy drained than a lighter
    turn -- the behavioral core the task exists to prove: token-driven, not
    flat."""
    light_state = MindState()
    light = make_mindloop(
        memory_root=tmp_path / "light",
        state=light_state,
        queue=_user_turn_queue(),
        llm=_UsageLLM("hi", prompt=50, completion=100),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    await light.iterate()
    light_drain = 1.0 - light_state.energy

    heavy_state = MindState()
    heavy = make_mindloop(
        memory_root=tmp_path / "heavy",
        state=heavy_state,
        queue=_user_turn_queue(),
        llm=_UsageLLM("hi, at length", prompt=400, completion=1000),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    await heavy.iterate()
    heavy_drain = 1.0 - heavy_state.energy

    assert heavy_drain > light_drain
    assert light._turn_cost_mode == "measured"
    assert heavy._turn_cost_mode == "measured"


@pytest.mark.asyncio
async def test_partial_usage_still_measured(tmp_path):
    """Only ONE of (prompt, completion) reported this turn -> still measured
    (flat_legacy is reserved for BOTH being None all turn, per D1)."""
    state = MindState()
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=_user_turn_queue(),
        llm=_UsageLLM("hi", prompt=None, completion=500),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    await ml.iterate()

    # (500 + 0.25*0) / 2000 = 0.25
    assert state.energy == pytest.approx(1.0 - 0.25)
    assert ml._turn_cost_mode == "measured"
    assert ml._turn_tokens_total == 500


@pytest.mark.asyncio
async def test_missing_tokens_uses_flat_legacy(tmp_path):
    """No pass reported usage at all (the shared _FakeLLM never calls
    on_usage) -> D1's sanctioned no-fallback degrade: plain cost_per_turn,
    tagged flat_legacy."""
    state = MindState()
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=_user_turn_queue(),
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nHello there"),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    assert state.energy == 1.0
    await ml.iterate()

    assert state.energy == pytest.approx(1.0 - 0.05)
    assert ml._turn_cost_mode == "flat_legacy"
    assert ml._turn_tokens_total is None
    assert ml._turn_energy_cost == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_multi_pass_accumulates_before_single_drain(tmp_path):
    """A turn with a tool-call re-feed makes TWO cascade passes (pass 1 via
    stream_completion, pass 2 via stream_messages after the Recall result is
    fed back — mirrors
    tests/test_mind_loop.py::test_recall_result_refed_in_same_turn).
    on_usage fires once per pass; the drain site must SUM both passes and
    drain exactly ONCE, not per-pass."""
    scripts = [_recall_pass(speech="查一下"), _speech_pass("找到了")]
    usages = [(100, 200), (50, 80)]  # (prompt, completion) per pass

    state = MindState()
    ml = make_mindloop(
        memory_root=tmp_path,
        state=state,
        queue=_user_turn_queue(),
        llm=_ScriptedLLM(scripts, usages=usages),
        energy_enabled=True,
        cost_per_turn=0.05,
        token_per_energy_unit=2000.0,
    )
    await ml.iterate()

    # sum: prompt=150, completion=280 -> (280 + 0.25*150)/2000 = 317.5/2000
    expected = (280 + 0.25 * 150) / 2000.0
    assert state.energy == pytest.approx(1.0 - expected)
    assert ml._turn_cost_mode == "measured"
    assert ml._turn_tokens_total == 150 + 280
