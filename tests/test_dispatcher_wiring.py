"""Tests for EventDispatcher — runner wiring into ToolCtx.

Covers: subagent_runner, shell_runner threading into tool ctx.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from dollos.dispatcher import EventDispatcher
from dollos.events import UserTextEvent
from dollos.llm.adapter import StreamChunk
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
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


@pytest.mark.parametrize(
    "runner_field,runner_factory",
    [
        ("subagent_runner", lambda tmp_path: MagicMock(name="FAKE_SUBAGENT_RUNNER")),
        (
            "shell_runner",
            lambda tmp_path: __import__("dollos.shell_runner", fromlist=["ShellRunner"])
            .ShellRunner(
                cwd=tmp_path,
                tool_output_store=__import__("dollos.tool_outputs", fromlist=["ToolOutputStore"])
                .ToolOutputStore(tmp_path / "tool_outputs"),
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_dispatcher_threads_runner_into_tool_ctx(
    tmp_path: Path, runner_field: str, runner_factory
) -> None:
    """Each runner kwarg is passed through into ToolCtx during a cascade."""
    from dollos.tools import MAIN_TOOLS

    captured: list[object] = []

    class _CaptureTool(BaseModel):
        async def run(self, ctx: ToolCtx):
            # Capture the runner field requested by this parametrization.
            captured.append(getattr(ctx, runner_field))
            return None

    runner = runner_factory(tmp_path)

    # For shell_runner, must replace MAIN_TOOLS since it captures ctx.
    TOOLS_orig = list(MAIN_TOOLS) if runner_field == "shell_runner" else None
    if runner_field == "shell_runner":
        MAIN_TOOLS.clear()
        MAIN_TOOLS.extend([_CaptureTool])

    try:
        adapter = _FakeAdapter(
            chunks=[
                StreamChunk(
                    text='<tool_call>{"name":"_CaptureTool","arguments":{}}</tool_call>',
                    done=True,
                ),
            ]
        )
        iv = _FakeInnerVoice()
        inst = _FakeInstinct(summaries=[""])
        ms = _FakeMemSearch()

        kwargs = dict(
            adapter=adapter,
            inner_voice=iv,
            instinct=inst,
            renderer=PromptRenderer(),
            identity=_doll_identity("x"),
            memory_root=tmp_path,
            memsearch=ms,
            transcripts_root=tmp_path / "transcripts",
            cascade_logger=_FakeCascadeLogger(),
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        )
        kwargs[runner_field] = runner

        dispatcher = EventDispatcher(**kwargs)
        if runner_field == "subagent_runner":
            dispatcher._tools_by_name["_CaptureTool"] = _CaptureTool

        sink: asyncio.Queue = asyncio.Queue()
        dispatcher.dispatch(UserTextEvent(text="test", response_sink=sink))
        await _drain(sink)

        assert len(captured) == 1, f"tool not invoked; {runner_field}"
        assert captured[0] is runner, f"runner mismatch for {runner_field}"
    finally:
        if TOOLS_orig is not None:
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
