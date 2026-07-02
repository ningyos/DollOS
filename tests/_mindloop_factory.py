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
) -> MindLoop:
    """Minimal MindLoop construction with stub queue/llm/ctx, for composition
    seam tests. ``ctx.memory_root`` is set to the passed ``memory_root``."""
    state = MindState()
    queue = PerceptionQueue(wal=None)
    tool_registry = {cls.__name__: cls for cls in MAIN_TOOLS}
    ctx = _make_mind_ctx(memory_root, state=state)
    llm = _FakeLLM(
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
    )
