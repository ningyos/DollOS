"""Shared fakes + factory functions for MindLoop / tool tests."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from dollos.character import Identity
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.tool_outputs import ToolOutputStore


def _doll_identity(self_: str = "You are Doll.") -> Identity:
    return Identity(self=self_, personality="- chill", taboos="- no LARP")


@dataclass
class _FakeAdapter(LLMAdapter):
    """Fake LLMAdapter — yields a configurable sequence of chunks.

    Captures call args for assertions. Records each call's keyword args
    in `self.calls`. For dispatcher (multi-message) tests the relevant
    entry is `calls[i]["messages"]`; legacy `stream_completion` callers
    populate `calls[i]["user"]` / `calls[i]["prefill"]` for back-compat
    with small-model code paths in tests.
    """

    chunks: list[StreamChunk] = field(default_factory=list)
    delay: float = 0.0
    calls: list[dict] = field(default_factory=list)

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "user": user, "prefill": prefill, "tools": tools}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        for c in self.chunks:
            yield c

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": tools}
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        for c in self.chunks:
            yield c


class _HangAdapter(LLMAdapter):
    """Adapter that hangs forever (for stop()/cancel tests)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
    ) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await asyncio.Event().wait()  # forever
        yield StreamChunk(text="", done=True)  # pragma: no cover

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools: list[type] | None = None,
        grammar: str | None = None,
        purpose: str = "cascade",
    ) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await asyncio.Event().wait()  # forever
        yield StreamChunk(text="", done=True)  # pragma: no cover


class _FakeMemSearch:
    def __init__(self, hits: list | None = None) -> None:
        self.indexed: list = []
        self._hits = hits or []

    async def index_file(self, path):
        self.indexed.append(path)

    async def search(self, query: str, top_k: int = 5):
        return self._hits


class _FakeCascadeLogger:
    """Records start_turn/log_iter calls for assertion."""

    def __init__(self) -> None:
        self.turn_ids: list[str] = []
        self.iters: list[dict] = []

    def start_turn(self) -> str:
        tid = f"fake-turn-{len(self.turn_ids) + 1}"
        self.turn_ids.append(tid)
        return tid

    def log_iter(self, **kwargs) -> None:
        self.iters.append(dict(kwargs))


def _make_mind_ctx(
    tmp_path: Path,
    memsearch=None,
    sink=None,
    state=None,
    shell_runner=None,
    workflow_runner=None,
    monitor_runner=None,
    tool_output_store=None,
):
    """Factory for MindCtx — for use in tool tests and future MindLoop tests."""
    from dollos.mind.mind_ctx import MindCtx
    from dollos.mind.mind_state import MindState
    from dollos.mind.sink_resolver import SinkResolver

    resolver = SinkResolver()
    if sink is not None:
        resolver.register(sink)

    class _FakeShellRunner:
        def __init__(self):
            self.calls = []
        def spawn(self, **kwargs):
            self.calls.append(kwargs)

    class _FakeWorkflowRunner:
        def __init__(self):
            self.calls = []
        def spawn(self, *, workflow_id, tasks, synthesis=None, mode="map_reduce", timeout_s=600, response_sink=None):
            self.calls.append(dict(workflow_id=workflow_id, tasks=tasks, synthesis=synthesis, mode=mode, timeout_s=timeout_s))

    class _FakeMonitorRunner:
        def __init__(self):
            self.calls = []
            self._monitor_id = "mon-1"
        def spawn(self, **kwargs):
            self.calls.append(kwargs)
            return self._monitor_id
        async def remove(self, monitor_id):
            return True

    return MindCtx(
        mind_state=state or MindState(),
        memsearch=memsearch or _FakeMemSearch(),
        memory_root=tmp_path,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=resolver,
        tool_output_store=tool_output_store or ToolOutputStore(tmp_path / "tool_outputs"),
        shell_runner=shell_runner or _FakeShellRunner(),
        workflow_runner=workflow_runner or _FakeWorkflowRunner(),
        monitor_runner=monitor_runner or _FakeMonitorRunner(),
    )


async def _drain(sink: asyncio.Queue) -> list:
    items: list = []
    async with asyncio.timeout(1.0):
        while True:
            item = await sink.get()
            items.append(item)
            if item is None:
                return items
    return items  # pragma: no cover


