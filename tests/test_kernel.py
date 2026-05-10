"""Integration tests for DollOS._handle_text_input via EventDispatcher."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dollos.config import (
    CharacterConfig,
    DataConfig,
    InnerVoiceConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemsearchConfig,
    Settings,
)
from dollos.ipc.messages import ErrorMsg, TextChunk, TextInput, TurnEnd
from dollos.kernel import DollOS
from dollos.llm.adapter import LLMAdapter, StreamChunk


def _make_settings(tmp_path: Path) -> Settings:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "doll.toml").write_text(
        '[meta]\n'
        'id = "doll"\n'
        'name = "Doll"\n'
        '\n'
        '[identity]\n'
        'self = "You are Doll."\n'
        'personality = "- chill"\n'
        'taboos = "- no LARP"\n'
    )
    return Settings(
        llm=LLMConfig(
            provider="llamacpp",
            template="qwen3-thinking",
            base_url="http://test.local:8001",
            model_alias="big",
        ),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        log=LogConfig(level="WARNING"),
        data=DataConfig(root=tmp_path / "data"),
        memsearch=MemsearchConfig(top_k=7),
        character=CharacterConfig(pack=pack_dir),
        inner_voice=InnerVoiceConfig(
            base_url="http://test.local:8003",
            timeout_s=15.0,
        ),
    )


@dataclass
class _FakeAdapter(LLMAdapter):
    chunks: list[StreamChunk] = field(default_factory=list)
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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "user": user, "prefill": prefill})
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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "messages": list(messages)})
        for c in self.chunks:
            yield c


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list = []

    async def index_file(self, path):
        self.indexed.append(path)


def _install_fake_inner_voice(monkeypatch, recall_text: str | None = None, raises=None):
    captured: list[str] = []

    async def _stub_recall(self, query, **kwargs):
        captured.append(query)
        if raises is not None:
            raise raises
        return recall_text if recall_text is not None else "- foo"

    async def _stub_instinct_process(self, event):
        return ""

    async def _stub_compact_cascade(self, *, perception, cascade_messages):
        return "test summary"

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_instinct_process)
    monkeypatch.setattr(
        "dollos.instinct.SmallModelInstinct.compact_cascade", _stub_compact_cascade
    )
    return captured


@pytest.fixture
def dollos_with_fakes(tmp_path, monkeypatch):
    """Build a DollOS with fake adapter swapped in. Returns (dollos, adapter,
    iv_calls)."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    fake_adapter = _FakeAdapter()
    dollos.adapter = fake_adapter
    # Re-build dispatcher to point at the fake adapter.
    from dollos.dispatcher import EventDispatcher

    dollos.dispatcher = EventDispatcher(
        adapter=fake_adapter,
        inner_voice=dollos.inner_voice,
        instinct=dollos.instinct,
        renderer=dollos.renderer,
        identity=dollos._doll_pack.identity,
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
    )
    return dollos, fake_adapter


async def _drain_until_separator(sink: asyncio.Queue) -> list:
    """Drain a sink until the first None separator (turn boundary)."""
    out = []
    while True:
        item = await sink.get()
        if item is None:
            return out
        out.append(item)


@pytest.mark.asyncio
async def test_handle_text_input_yields_chunks_then_turnend(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [
        StreamChunk(
            text='<tool_call>{"name":"Say","arguments":{"text":"ok"}}</tool_call>',
            done=False,
        ),
        StreamChunk(text="", done=True),
    ]
    _install_fake_inner_voice(monkeypatch, "- foo")

    sink: asyncio.Queue = asyncio.Queue()
    result = await dollos._handle_text_input(TextInput(text="hi"), sink)
    assert result is None  # void return; output flows via sink
    items = await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert any(isinstance(m, TextChunk) and m.text == "ok" for m in items)
    assert isinstance(items[-1], TurnEnd)


@pytest.mark.asyncio
async def test_handle_text_input_yields_errormsg_on_dispatch_failure(
    dollos_with_fakes, monkeypatch
):
    dollos, _adapter = dollos_with_fakes
    _install_fake_inner_voice(monkeypatch, raises=RuntimeError("boom"))

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_text_input(TextInput(text="x"), sink)
    items = await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert len(items) == 1
    assert isinstance(items[0], ErrorMsg)
    assert "boom" in items[0].message


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_stream_messages(
    dollos_with_fakes, monkeypatch
):
    """Cascade uses multi-message API (2026-05-08); legacy stream_completion
    is reserved for small-model callers."""
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch, "- foo")

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_text_input(TextInput(text="hi"), sink)
    await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    assert len(adapter.calls) == 1
    assert "messages" in adapter.calls[0]
    assert "prefill" not in adapter.calls[0]


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_text_as_user_role(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch)

    sink: asyncio.Queue = asyncio.Queue()
    await dollos._handle_text_input(TextInput(text="hello world"), sink)
    await asyncio.wait_for(_drain_until_separator(sink), timeout=2.0)
    user = adapter.calls[0]["messages"][0]["content"]
    assert "[Memory context]" in user
    assert "[Message]" in user
    assert "hello world" in user


@pytest.mark.asyncio
async def test_drain_diary_sink_consumes_until_sentinel(tmp_path):
    """_drain_diary_sink eats messages and returns on None sentinel."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)
    sink: asyncio.Queue = asyncio.Queue()
    sink.put_nowait(TextChunk(text="ignored"))
    sink.put_nowait(ErrorMsg(message="logged"))
    sink.put_nowait(TurnEnd())
    sink.put_nowait(None)
    await asyncio.wait_for(dollos._drain_diary_sink(sink), timeout=1.0)


@pytest.mark.asyncio
async def test_diary_scheduler_returns_on_shutdown(tmp_path):
    """Scheduler returns when shutdown is set, even if next fire is far away."""
    settings = _make_settings(tmp_path)
    dollos = DollOS(settings)

    async def _quickshutdown():
        await asyncio.sleep(0.05)
        dollos._shutdown.set()

    asyncio.create_task(_quickshutdown())
    await asyncio.wait_for(dollos._diary_scheduler(), timeout=2.0)
