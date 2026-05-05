"""Integration tests for DollOS._handle_text_input via EventDispatcher."""

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
    character_path = tmp_path / "character.jinja"
    character_path.write_text("You are Doll.")
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
        character=CharacterConfig(profile_path=character_path),
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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "user": user, "prefill": prefill})
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
        return recall_text or "RECALL:\n- foo\n"

    async def _stub_instinct_process(self, event):
        return ""

    monkeypatch.setattr("dollos.inner_voice.InnerVoice.recall", _stub_recall)
    monkeypatch.setattr("dollos.instinct.SmallModelInstinct.process", _stub_instinct_process)
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
        character_profile=dollos._character_profile,
        memory_root=tmp_path,
        memsearch=_FakeMemSearch(),
    )
    return dollos, fake_adapter


async def _collect(it):
    out = []
    async for item in it:
        out.append(item)
    return out


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
    _install_fake_inner_voice(monkeypatch, "RECALL:\n- foo\n")

    items = await _collect(dollos._handle_text_input(TextInput(text="hi")))
    assert any(isinstance(m, TextChunk) and m.text == "ok" for m in items)
    assert isinstance(items[-1], TurnEnd)


@pytest.mark.asyncio
async def test_handle_text_input_yields_errormsg_on_dispatch_failure(
    dollos_with_fakes, monkeypatch
):
    dollos, _adapter = dollos_with_fakes
    _install_fake_inner_voice(monkeypatch, raises=RuntimeError("boom"))

    items = await _collect(dollos._handle_text_input(TextInput(text="x")))
    assert len(items) == 1
    assert isinstance(items[0], ErrorMsg)
    assert "boom" in items[0].message


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_recall_in_prefill(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch, "RECALL:\n- foo\n")

    await _collect(dollos._handle_text_input(TextInput(text="hi")))
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["prefill"] == "RECALL:\n- foo\nDECISION: "


@pytest.mark.asyncio
async def test_dispatch_user_text_uses_text_as_user_role(
    dollos_with_fakes, monkeypatch
):
    dollos, adapter = dollos_with_fakes
    adapter.chunks = [StreamChunk(text="", done=True)]
    _install_fake_inner_voice(monkeypatch)

    await _collect(dollos._handle_text_input(TextInput(text="hello world")))
    assert adapter.calls[0]["user"] == "hello world"
