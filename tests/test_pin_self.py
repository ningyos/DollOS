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
    ctx = make_mind_ctx(self_profile_max_chars=80)
    await PinSelf(section="self", op="add", target="", text="字" * 40).run(ctx)
    msg = await PinSelf(section="self", op="add", target="", text="字" * 40).run(ctx)
    assert "上限" in msg
