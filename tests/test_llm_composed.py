"""Tests for ComposedLLMAdapter wiring."""

from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, Field

from dollos.llm.adapter import StreamChunk
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import PromptTemplate
from dollos.llm.transport import Provider


class _FakeTemplate(PromptTemplate):
    """Renders prompt as 'SYS={system}|USR={user}|PRE={prefill}' for assertion."""

    def render(self, *, system: str, user: str, prefill: str, tools=None) -> str:
        return f"SYS={system}|USR={user}|PRE={prefill}"

    def render_messages(self, *, system: str, messages: list[dict], tools=None) -> str:
        body = "|".join(f"{m['role']}={m['content']}" for m in messages)
        tool_marker = ""
        if tools:
            tool_marker = "|TOOLS=" + ",".join(t.__name__ for t in tools)
        return f"SYS={system}|MSG={body}{tool_marker}"


class _FakeProvider(Provider):
    """Captures the prompt it receives and yields canned chunks."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.last_prompt: str | None = None
        self.last_stop: list[str] | None = None
        self.last_max_tokens: int | None = None
        self.last_grammar: str | None = None

    @property
    def supports_prefill(self) -> bool:
        return True

    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        grammar: str | None = None,
        purpose: str = "cascade",
        on_usage=None,
    ) -> AsyncIterator[StreamChunk]:
        self.last_prompt = prompt
        self.last_stop = stop
        self.last_max_tokens = max_tokens
        self.last_grammar = grammar
        self.last_purpose = purpose
        self.last_on_usage = on_usage
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_composed_calls_template_then_provider():
    fake_chunks = [
        StreamChunk(text="ok", done=False),
        StreamChunk(text="", done=True),
    ]
    provider = _FakeProvider(fake_chunks)
    template = _FakeTemplate()
    adapter = ComposedLLMAdapter(provider=provider, template=template)

    out = []
    async for chunk in adapter.stream_completion(
        system="S",
        user="U",
        prefill="P",
        stop=["<|end|>"],
        max_tokens=42,
    ):
        out.append(chunk)

    # Template was applied and result handed to provider verbatim.
    assert provider.last_prompt == "SYS=S|USR=U|PRE=P"
    # Stop and max_tokens forwarded through.
    assert provider.last_stop == ["<|end|>"]
    assert provider.last_max_tokens == 42
    # Yielded chunks come straight from the provider.
    assert out == fake_chunks


@pytest.mark.asyncio
async def test_composed_default_prefill_is_empty():
    provider = _FakeProvider([StreamChunk(text="", done=True)])
    adapter = ComposedLLMAdapter(provider=provider, template=_FakeTemplate())

    async for _ in adapter.stream_completion(system="S", user="U"):
        pass

    # Default prefill is "".
    assert provider.last_prompt == "SYS=S|USR=U|PRE="


@pytest.mark.asyncio
async def test_composed_default_stop_and_max_tokens():
    provider = _FakeProvider([StreamChunk(text="", done=True)])
    adapter = ComposedLLMAdapter(provider=provider, template=_FakeTemplate())

    async for _ in adapter.stream_completion(system="S", user="U", prefill=""):
        pass

    # Defaults: stop=None, max_tokens=1024 forwarded through.
    assert provider.last_stop is None
    assert provider.last_max_tokens == 1024


class _ToolDummy(BaseModel):
    """Dummy tool."""

    text: str = Field(description="x")


@pytest.mark.asyncio
async def test_composed_passes_tools_to_template():
    """ComposedLLMAdapter forwards `tools=` into template.render()."""

    captured = {}

    class _CaptureTemplate:
        def render(self, *, system, user, prefill, tools=None):
            captured["tools"] = tools
            return "RENDERED"

    class _StubProvider:
        async def stream(
            self, *, prompt, stop=None, max_tokens=1024, grammar=None,
            purpose="cascade", on_usage=None,
        ):
            captured["prompt"] = prompt
            yield StreamChunk(text="ok", done=True)

    adapter = ComposedLLMAdapter(
        provider=_StubProvider(), template=_CaptureTemplate()
    )
    chunks = []
    async for ch in adapter.stream_completion(
        system="s", user="u", prefill="", tools=[_ToolDummy]
    ):
        chunks.append(ch)

    assert captured["tools"] == [_ToolDummy]
    assert captured["prompt"] == "RENDERED"


@pytest.mark.asyncio
async def test_composed_stream_messages_renders_via_template_and_calls_provider():
    """ComposedLLMAdapter.stream_messages must render via
    template.render_messages() and forward the rendered prompt + grammar
    to provider.stream()."""
    fake_chunks = [
        StreamChunk(text="hi", done=False),
        StreamChunk(text="", done=True),
    ]
    provider = _FakeProvider(fake_chunks)
    template = _FakeTemplate()
    adapter = ComposedLLMAdapter(provider=provider, template=template)

    out = []
    async for chunk in adapter.stream_messages(
        system="S",
        messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "<tool_response>\nx\n</tool_response>"},
        ],
        stop=["<|end|>"],
        max_tokens=99,
        tools=[_ToolDummy],
        grammar="GBNF",
    ):
        out.append(chunk)

    assert provider.last_prompt == (
        "SYS=S|MSG=user=u1|assistant=a1|user=<tool_response>\nx\n</tool_response>"
        "|TOOLS=_ToolDummy"
    )
    assert provider.last_stop == ["<|end|>"]
    assert provider.last_max_tokens == 99
    assert provider.last_grammar == "GBNF"
    assert out == fake_chunks
