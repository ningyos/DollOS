"""Tests for EventDispatcher — runner wiring into ToolCtx.

Covers: subagent_runner, shell_runner, monitor_runner threading into tool ctx.
"""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from dollos.dispatcher import EventDispatcher
from dollos.events import UserTextEvent
from dollos.llm.adapter import StreamChunk
from dollos.prompts import PromptRenderer
from dollos.tools import ToolCtx

from tests._dispatcher_helpers import (
    _FakeAdapter,
    _FakeCascadeLogger,
    _FakeInstinct,
    _FakeInnerVoice,
    _FakeMemSearch,
    _doll_identity,
    _drain,
    _make_dispatcher,
)


@pytest.mark.asyncio
async def test_dispatcher_passes_subagent_runner_into_tool_ctx(tmp_path: Path):
    """When a subagent_runner is wired, _respond's ToolCtx carries it
    through so SpawnSubagent.run() can dispatch new tasks."""

    captured: list[object] = []

    class _CaptureRunnerTool(BaseModel):
        async def run(self, ctx) -> None:
            # Side-effect capture; return None so cascade ends after this iter.
            captured.append(ctx.subagent_runner)

    class _FakeRunner:
        def __repr__(self) -> str:
            return "FAKE_RUNNER"

    runner = _FakeRunner()
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(
                text='<tool_call>{"name":"_CaptureRunner","arguments":{}}</tool_call>',
                done=True,
            ),
        ]
    )
    iv = _FakeInnerVoice()
    inst = _FakeInstinct(summaries=[""])
    ms = _FakeMemSearch()
    disp = EventDispatcher(
        adapter=adapter,
        inner_voice=iv,
        instinct=inst,
        renderer=PromptRenderer(),
        identity=_doll_identity("x"),
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        cascade_logger=_FakeCascadeLogger(),
        subagent_runner=runner,  # type: ignore[arg-type]
    )
    disp._tools_by_name["_CaptureRunner"] = _CaptureRunnerTool

    sink: asyncio.Queue = asyncio.Queue()
    disp.dispatch(UserTextEvent(text="hi", response_sink=sink))
    await _drain(sink)
    # Tool ran exactly once and saw `runner` in ctx.
    assert captured == [runner]


@pytest.mark.asyncio
async def test_dispatcher_threads_shell_runner_into_tool_ctx(tmp_path: Path):
    """EventDispatcher is built with a ShellRunner; that exact instance
    must reach tool ctx during a cascade."""
    from dollos.shell_runner import ShellRunner
    from dollos.tools import MAIN_TOOLS

    captured: list[ToolCtx] = []

    class _CaptureTool(BaseModel):
        token: str = "x"

        async def run(self, ctx: ToolCtx):  # noqa: D401
            captured.append(ctx)
            return None

    TOOLS_orig = list(MAIN_TOOLS)
    MAIN_TOOLS.clear()
    MAIN_TOOLS.extend([_CaptureTool])
    try:
        adapter = _FakeAdapter(
            chunks=[
                StreamChunk(
                    text='<tool_call>{"name":"_CaptureTool","arguments":{"token":"x"}}</tool_call>',
                    done=False,
                ),
                StreamChunk(text="", done=True),
            ],
        )
        iv = _FakeInnerVoice()
        runner = ShellRunner(cwd=tmp_path)
        dispatcher = EventDispatcher(
            adapter=adapter,
            inner_voice=iv,
            instinct=_FakeInstinct(),
            renderer=PromptRenderer(),
            identity=_doll_identity(),
            memory_root=tmp_path,
            memsearch=_FakeMemSearch(),
            transcripts_root=tmp_path / "transcripts",
            cascade_logger=_FakeCascadeLogger(),
            shell_runner=runner,
        )

        sink: asyncio.Queue = asyncio.Queue()
        dispatcher.dispatch(UserTextEvent(text="go", response_sink=sink))
        await _drain(sink)

        assert captured, "tool was not invoked"
        assert captured[0].shell_runner is runner
    finally:
        MAIN_TOOLS.clear()
        MAIN_TOOLS.extend(TOOLS_orig)


def test_dispatcher_accepts_no_shell_runner(tmp_path: Path):
    """Default ctor: shell_runner is None (back-compat with tests)."""
    iv = _FakeInnerVoice()
    adapter = _FakeAdapter(chunks=[StreamChunk(text="", done=True)])
    dispatcher = _make_dispatcher(
        adapter=adapter, inner_voice=iv, tmp_path=tmp_path
    )
    assert dispatcher._shell_runner is None
