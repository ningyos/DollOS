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
    """Recall format_hit places [系統整併·待確認] prefix BEFORE the date (spec §3.3)."""
    from dollos.tools import _format_hit
    h_cons = {"content": "主人偏好冰美式", "source": "shared/consolidated/2026-06-29.md"}
    h_norm = {"content": "今天天氣好", "source": "shared/2026-06-30.md"}
    cons_result = _format_hit(h_cons)
    norm_result = _format_hit(h_norm)
    # Prefix present in consolidated hit
    assert "[系統整併·待確認]" in cons_result
    # Prefix is before the date — bullet content starts with prefix, not date
    assert cons_result.startswith("- [系統整併·待確認]"), (
        f"Expected prefix before date, got: {cons_result!r}"
    )
    # The date still appears, but after the prefix
    prefix_pos = cons_result.index("[系統整併·待確認]")
    date_pos = cons_result.index("2026-06-29")
    assert prefix_pos < date_pos, (
        f"Prefix should appear before date in: {cons_result!r}"
    )
    # Normal hits have no prefix
    assert "[系統整併·待確認]" not in norm_result


@pytest.mark.asyncio
async def test_associative_search_excludes_consolidated(tmp_path):
    """associative_search must not surface consolidated/ hits (pull-only gating)."""
    from datetime import datetime
    from dollos.mind.associative_search import associative_search
    from dollos.mind.mind_state import MindState, Perception

    state = MindState()
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=1.0, data={"text": "mood test"})
    )

    # At 14:00 on 2026-06-29 (Sunday), tod=afternoon, dow=Sun.
    # Heading format: "YYYY-MM-DD HH:MM:SS [key:value ...]" — colon separator.
    matching_heading = "2026-06-29 14:00:00 [tod:afternoon]"

    class _MixedMemSearch:
        async def search(self, query, top_k=5):
            return [
                # consolidated hit — tagged with tod:afternoon so it would
                # match the tod axis if not filtered out first.
                {
                    "content": "consolidated fact",
                    "source": "shared/consolidated/2026-06-29.md",
                    "heading": matching_heading,
                    "chunk_hash": "hash-consolidated",
                },
                # normal hit — same heading tags, should pass through.
                {
                    "content": "normal fact",
                    "source": "shared/2026-06-29.md",
                    "heading": matching_heading,
                    "chunk_hash": "hash-normal",
                },
            ]

    results = await associative_search(
        _MixedMemSearch(),
        state,
        top_k=5,
        now=datetime(2026, 6, 29, 14, 0, 0),
    )
    contents = [r["content"] for r in results]
    assert "consolidated fact" not in contents, (
        "consolidated/ hit must not appear in associative_search results"
    )
    assert "normal fact" in contents, (
        "non-consolidated hit with matching tags must still be returned"
    )


# ---------------------------------------------------------------------------
# Shared stub for Task 4 + 5
# ---------------------------------------------------------------------------

class _FakeRenderer:
    """Minimal PromptRenderer stub for consolidation tests."""
    def render(self, template_name: str, **ctx) -> str:
        return f"[stub system: {template_name}]"


# ---------------------------------------------------------------------------
# Task 4: KEEPER_TOOLS allowlist + run_consolidation driver
# ---------------------------------------------------------------------------


def test_keeper_tools_allowlist():
    from dollos.tools import KEEPER_TOOLS, Report, Scratchpad, Shell, NoteMemory, SpawnMonitor, RemoveMonitor
    names = {c.__name__ for c in KEEPER_TOOLS}
    assert names <= {"Report", "Scratchpad", "Recall"}
    assert names & {"Shell", "NoteMemory", "SpawnMonitor", "RemoveMonitor"} == set()
    assert Report in KEEPER_TOOLS


@pytest.mark.asyncio
async def test_run_consolidation_writes_candidate_file(tmp_path, monkeypatch):
    from dollos.mind import consolidation as C
    # 準備目標日 transcript
    tdir = tmp_path / "transcripts"; tdir.mkdir()
    (tdir / "2026-06-29.md").write_text("- 12:00 主人說：我喜歡冰美式\n- 12:01 我說：記住了\n")
    ms = _FakeMemSearch()

    captured = {}
    async def fake_run_agent(**kw):
        captured.update(kw)
        return {"status": "ok", "details": "- 主人偏好冰美式"}
    monkeypatch.setattr(C, "run_agent", fake_run_agent)

    ok = await C.run_consolidation(
        target_date="2026-06-29",
        adapter=object(), renderer=_FakeRenderer(), memsearch=ms,
        memory_root=tmp_path, transcripts_root=tdir,
        tool_output_store=object(), consolidated_dir=tmp_path / "consolidated",
        max_tokens=2048, agent_timeout_s=120, transcript_tail_chars=8000,
    )
    assert ok is True
    out = (tmp_path / "consolidated" / "2026-06-29.md").read_text()
    assert "主人偏好冰美式" in out
    # transcript 內容有 inline 進 task（driver-fed）
    assert "冰美式" in captured["task"]
    # keeper 用 allowlist + 無 shell_runner
    from dollos.tools import KEEPER_TOOLS
    assert captured["tools"] is KEEPER_TOOLS
    assert captured.get("shell_runner") is None
    # 寫的檔被索引
    assert (tmp_path / "consolidated" / "2026-06-29.md") in ms.indexed
