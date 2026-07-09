"""Shared MindLoop test factory — minimal construction with stub deps.

Mirrors tests/test_mind_loop.py's ``_FakeLLM`` + tests/_dispatcher_helpers.py's
``_make_mind_ctx`` (the pattern tests/test_mind_loop_self_profile.py's local
``_build_mind_loop`` replicated inline). Extracted here per Task 7 (composition
seam) so per-turn-compose tests don't duplicate construction again.
"""
from __future__ import annotations

from pathlib import Path

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState
from dollos.mind.perception_queue import PerceptionQueue
from dollos.tools import MAIN_TOOLS
from tests._dispatcher_helpers import _make_mind_ctx
from tests.test_mind_loop import _FakeLLM


def make_mindloop(
    *,
    memory_root: Path,
    system_prompt: str = "SYS",
    system_prompt_suffix: str = "",
    evolution_enabled: bool = False,
    current_self_min_chars: int = 80,
    current_self_max_chars: int = 600,
    self_profile_enabled: bool = False,
    pending_max_surfacings: int = 5,
    pending_min_age_days: float = 2.0,
    sink=None,
    queue: PerceptionQueue | None = None,
    state: MindState | None = None,
    llm=None,
    trace_writer=None,
    model_id: str | None = None,
    energy_enabled: bool = False,
    cost_per_turn: float = 0.05,
    on_turn_complete=None,
    diary_max_log_chars: int = 40000,
    turn_latency_recorder=None,
    cascade_logger=None,
) -> MindLoop:
    """Minimal MindLoop construction with stub queue/llm/ctx, for composition
    seam + wiring tests. ``ctx.memory_root`` is set to the passed ``memory_root``.

    Pass ``sink`` (an ``asyncio.Queue``) + a pre-seeded ``queue`` to drive a
    real ``iterate()`` (F6 wiring tests); the returned loop shares the caller's
    ``state`` when one is supplied so multi-iterate drives observe carried
    state (e.g. the drain-time latch reset)."""
    state = state if state is not None else MindState()
    queue = queue if queue is not None else PerceptionQueue(wal=None)
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(memory_root, sink=sink, state=state)
    llm = llm if llm is not None else _FakeLLM(
        "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\nhi"
    )
    return MindLoop(
        state=state,
        queue=queue,
        ctx=ctx,
        llm=llm,
        system_prompt=system_prompt,
        system_prompt_suffix=system_prompt_suffix,
        state_persist_path=memory_root / "mind_state.json",
        tool_registry=tool_registry,
        evolution_enabled=evolution_enabled,
        current_self_min_chars=current_self_min_chars,
        current_self_max_chars=current_self_max_chars,
        self_profile_enabled=self_profile_enabled,
        pending_max_surfacings=pending_max_surfacings,
        pending_min_age_days=pending_min_age_days,
        trace_writer=trace_writer,
        model_id=model_id,
        energy_enabled=energy_enabled,
        cost_per_turn=cost_per_turn,
        on_turn_complete=on_turn_complete,
        diary_max_log_chars=diary_max_log_chars,
        turn_latency_recorder=turn_latency_recorder,
        cascade_logger=cascade_logger,
    )
