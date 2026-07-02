"""Tests for PinSelf — reflection-gated self-profile pin/replace/remove tool."""

from pathlib import Path

import pytest

from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState
from dollos.mind.sink_resolver import SinkResolver
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import MAIN_TOOLS, REFLECTION_TOOLS, PinSelf


class _FakeShellRunner:
    def spawn(self, **kwargs):
        pass


class _FakeWorkflowRunner:
    def spawn(self, **kwargs):
        pass


class _FakeMonitorRunner:
    def spawn(self, **kwargs):
        return "mon-1"

    async def remove(self, monitor_id):
        return True


class _FakeMemSearch:
    """Fake memsearch — records every index_file call so tests can prove
    self_profile.md is NEVER indexed (always-inject, not recall)."""

    def __init__(self) -> None:
        self.indexed_sources: list[Path] = []

    async def index_file(self, path):
        self.indexed_sources.append(Path(path))

    async def search(self, query: str, top_k: int = 5):
        return []


@pytest.fixture
def make_mind_ctx(tmp_path):
    """Builds a real MindCtx with a tmp memory_root and a fake memsearch that
    records index_file calls. Everything else is the same real/near-real
    plumbing _dispatcher_helpers.py uses elsewhere (real MindState, real
    ToolOutputStore, fake shell/workflow/monitor runners)."""

    def _factory(self_profile_max_chars: int = 1200) -> MindCtx:
        return MindCtx(
            mind_state=MindState(),
            memsearch=_FakeMemSearch(),
            memory_root=tmp_path,
            transcripts_root=tmp_path / "transcripts",
            sink_resolver=SinkResolver(),
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
            shell_runner=_FakeShellRunner(),
            workflow_runner=_FakeWorkflowRunner(),
            monitor_runner=_FakeMonitorRunner(),
            self_profile_max_chars=self_profile_max_chars,
        )

    return _factory


def test_pinself_in_reflection_not_main():
    assert PinSelf in REFLECTION_TOOLS
    assert PinSelf not in MAIN_TOOLS


@pytest.mark.asyncio
async def test_pinself_add_writes_file_and_not_indexed(make_mind_ctx):
    ctx = make_mind_ctx()
    tool = PinSelf(section="self", op="add", target="", text="我重視誠實")
    msg = await tool.run(ctx)
    prof = ctx.memory_root / "self_profile.md"
    assert prof.exists()
    assert "我重視誠實" in prof.read_text()
    assert "s1" in msg
    # 絕不 index:fake memsearch 不應收到 self_profile.md,且根本沒被呼叫過。
    assert all("self_profile.md" not in str(s) for s in ctx.memsearch.indexed_sources)
    assert ctx.memsearch.indexed_sources == []


@pytest.mark.asyncio
async def test_pinself_cap_returns_friendly_error(make_mind_ctx):
    """Verify that self-profile cap is enforced only AFTER accumulation.

    With max_chars=120:
    - First add of 40 chars serializes to ~96 chars → succeeds (no error)
    - Second add of 40 chars would serialize to ~147 chars → fails (has '上限')
    This ensures we test the accumulate-then-cap path, not immediate cap-on-first."""
    ctx = make_mind_ctx(self_profile_max_chars=120)

    # First add should succeed
    msg1 = await PinSelf(section="self", op="add", target="", text="字" * 40).run(ctx)
    assert "上限" not in msg1, f"First add should not hit cap, but got: {msg1}"
    assert "s1" in msg1, f"First add should return success with id s1, but got: {msg1}"

    # Second add should fail due to accumulated total
    msg2 = await PinSelf(section="self", op="add", target="", text="字" * 40).run(ctx)
    assert "上限" in msg2, f"Second add should hit cap, but got: {msg2}"


def test_pinself_docstring_states_subject_test_as_criterion():
    """Docstring's dividing line between PinSelf and NoteMemory is the
    sentence's subject (about-you vs. about-the-world), not durability."""
    doc = PinSelf.__doc__
    assert "主詞" in doc
    assert "NoteMemory" in doc


def test_pinself_docstring_allows_write_loose_nascent_entries():
    """Nascent/not-yet-sure-if-durable entries may be pinned — the write-time
    durability gate from the old docstring ('CORE, DURABLE truth... exactly
    three things') must be gone."""
    doc = PinSelf.__doc__
    assert "剛萌芽" in doc
    assert "CORE, DURABLE" not in doc
    assert "exactly three things" not in doc


def test_pinself_docstring_frames_prune_as_selection_not_hygiene():
    """Pruning is reframed as 'what survives is your core', not cleanup."""
    doc = PinSelf.__doc__
    assert "活下來的才是你的核心" in doc


def test_pinself_docstring_allows_dual_recording_same_experience():
    """One experience can produce a NoteMemory entry (the fact) AND a
    PinSelf entry (what it revealed about her) — this must be explicit."""
    doc = PinSelf.__doc__
    assert "兩邊各記一筆" in doc


def test_pinself_section_field_mentions_opinions_and_interests():
    desc = PinSelf.model_fields["section"].description
    assert "看法" in desc
    assert "興趣" in desc
    # relationship/user descriptions must stay exactly as before
    assert "relationship=你和主人" in desc
    assert "user=你注意到的主人" in desc
