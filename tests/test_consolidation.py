"""B2 sleep-time consolidation tests."""
from __future__ import annotations
import asyncio
from datetime import date
import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM
from dollos.tools import MAIN_TOOLS


def _bare_loop(tmp_path, state, ctx):
    """Minimal MindLoop for calling _derive_memory_hits directly (no iterate)."""
    queue = PerceptionQueue()
    return MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nhi"),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )


@pytest.mark.asyncio
async def test_user_turn_count_increments_only_on_userspoke(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "hi"}))
    ctx = _make_mind_ctx(tmp_path, sink=asyncio.Queue(), state=state)
    loop = MindLoop(
        state=state, queue=queue, ctx=ctx,
        llm=_FakeLLM("SEEN: x\nTOOL: none\n</think>\n\nhi"),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "s.json",
        tool_registry={c.__name__: c for c in MAIN_TOOLS},
    )
    await loop.iterate()
    assert state.user_turn_count == 1

    # A non-UserSpoke perception must NOT increment it
    queue.put(Perception(kind="ScheduledMoment", t=2.0, data={"text": "alarm"}))
    await loop.iterate()
    assert state.user_turn_count == 1


@pytest.mark.asyncio
async def test_consolidated_excluded_from_auto_inject(tmp_path):
    from dollos.mind.mind_state import MindState, Perception

    class _SrcMemSearch(_FakeMemSearch):
        async def search(self, query, top_k=5):
            return [
                {"content": "主人偏好冰美式", "source": "shared/consolidated/2026-06-29.md"},
                {"content": "今天天氣好", "source": "shared/2026-06-30.md"},
            ]

    state = MindState()
    state.recent_perceptions.append(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ctx = _make_mind_ctx(tmp_path, memsearch=_SrcMemSearch(), state=state)
    loop = _bare_loop(tmp_path, state=state, ctx=ctx)
    hits = await loop._derive_memory_hits()
    sources = [h.get("source", "") for h in hits]
    assert not any("consolidated/" in s for s in sources)
    assert any("shared/2026-06-30" in s for s in sources)


def test_recall_provenance_prefix_for_consolidated_hits():
    """Recall format_hit adds [系統整併·待確認] prefix for consolidated/ sources."""
    from dollos.tools import _format_hit
    h_cons = {"content": "主人偏好冰美式", "source": "shared/consolidated/2026-06-29.md"}
    h_norm = {"content": "今天天氣好", "source": "shared/2026-06-30.md"}
    assert "[系統整併·待確認]" in _format_hit(h_cons)
    assert "[系統整併·待確認]" not in _format_hit(h_norm)
