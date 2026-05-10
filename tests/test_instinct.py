"""Tests for Instinct ABC + SmallModelInstinct."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from dollos.events import DollEvent, UserTextEvent
from dollos.instinct import Instinct, SmallModelInstinct
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer


@dataclass
class _FakeAdapter(LLMAdapter):
    """Fake LLMAdapter — yields configured chunks; captures call args."""

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
        tools=None,
        grammar=None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({
            "system": system, "user": user, "prefill": prefill,
            "grammar": grammar,
        })
        for c in self.chunks:
            yield c

    async def stream_messages(
        self,
        *,
        system: str,
        messages: list[dict],
        stop: list[str] | None = None,
        max_tokens: int = 1024,
        tools=None,
        grammar=None,
    ) -> AsyncIterator[StreamChunk]:
        if False:  # pragma: no cover
            yield StreamChunk(text="", done=True)
        raise NotImplementedError("Instinct tests do not use stream_messages")


def _make_doll_event(text: str) -> DollEvent:
    raw = UserTextEvent(text=text, response_sink=asyncio.Queue())
    return DollEvent(perception=text, raw=raw)


def test_instinct_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        Instinct()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_small_model_instinct_first_call_uses_empty_prev_summary():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="主人說了 hi。", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    event = _make_doll_event("hi")

    summary = await inst.process(event)

    assert summary == "主人說了 hi。"
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert "(none — this is the first event)" in call["user"]
    assert "hi" in call["user"]
    assert call["prefill"] == ""


@pytest.mark.asyncio
async def test_small_model_instinct_persists_summary_across_calls():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="第一次摘要", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())

    s1 = await inst.process(_make_doll_event("first"))
    assert s1 == "第一次摘要"

    adapter.chunks = [
        StreamChunk(text="第二次摘要", done=False),
        StreamChunk(text="", done=True),
    ]
    s2 = await inst.process(_make_doll_event("second"))
    assert s2 == "第二次摘要"

    second_user = adapter.calls[1]["user"]
    assert "第一次摘要" in second_user
    assert "second" in second_user


@pytest.mark.asyncio
async def test_small_model_instinct_strips_whitespace():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="  trimmed  \n", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    summary = await inst.process(_make_doll_event("x"))
    assert summary == "trimmed"


@pytest.mark.asyncio
async def test_compact_cascade_returns_summary_string():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="我剛才回答了主人。\n", done=False),
            StreamChunk(text="", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())

    summary = await inst.compact_cascade(
        perception="hi",
        cascade_messages=[{"role": "assistant", "content": "<think>...</think>"}],
    )

    assert isinstance(summary, str)
    assert summary == "我剛才回答了主人。"
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["prefill"] == ""


@pytest.mark.asyncio
async def test_compact_cascade_strips_whitespace():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="  我做了某件事。  \n", done=True)]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    summary = await inst.compact_cascade(
        perception="hi", cascade_messages=[]
    )
    assert summary == "我做了某件事。"


@pytest.mark.asyncio
async def test_compact_cascade_passes_grammar_to_adapter():
    adapter = _FakeAdapter(
        chunks=[
            StreamChunk(text="ok\n", done=True),
        ]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    await inst.compact_cascade(
        perception="hi",
        cascade_messages=[],
    )
    grammar = adapter.calls[0]["grammar"]
    assert grammar is not None
    assert "line ::=" in grammar
    assert "{1,150}" in grammar


@pytest.mark.asyncio
async def test_compact_cascade_renders_jinja_blocks():
    """Spy renderer asserts compact_cascade hits the iv_compact template
    with perception + cascade_messages args."""
    captured: list[dict] = []
    real = PromptRenderer()

    class _SpyRenderer:
        def render(self, *a, **kw):
            return real.render(*a, **kw)

        def render_blocks(self, template_name, **ctx):
            captured.append({"template": template_name, "ctx": ctx})
            return real.render_blocks(template_name, **ctx)

    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="ok\n", done=True)]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=_SpyRenderer())

    msgs = [{"role": "user", "content": "[Message]\nq"}]
    await inst.compact_cascade(
        perception="q", cascade_messages=msgs
    )

    assert any(c["template"] == "iv_compact" for c in captured)
    compact_call = next(c for c in captured if c["template"] == "iv_compact")
    assert compact_call["ctx"]["perception"] == "q"
    assert compact_call["ctx"]["cascade_messages"] is msgs
    assert "prior_mood" not in compact_call["ctx"]


@pytest.mark.asyncio
async def test_compact_cascade_passes_messages_to_user_block():
    """Render the actual template and confirm cascade_messages content
    appears in the rendered user block."""
    renderer = PromptRenderer()
    msgs = [
        {"role": "assistant", "content": "thinking_about_coffee"},
        {"role": "user", "content": "<tool_response>\nok\n</tool_response>"},
    ]
    blocks = renderer.render_blocks(
        "iv_compact",
        perception="主人愛喝什麼",
        cascade_messages=msgs,
    )
    assert "主人愛喝什麼" in blocks["user"]
    assert "thinking_about_coffee" in blocks["user"]
    assert "<tool_response>" in blocks["user"]


@pytest.mark.asyncio
async def test_small_model_instinct_empty_output_is_empty_summary():
    adapter = _FakeAdapter(
        chunks=[StreamChunk(text="", done=True)]
    )
    inst = SmallModelInstinct(adapter=adapter, renderer=PromptRenderer())
    summary = await inst.process(_make_doll_event("x"))
    assert summary == ""

    adapter.chunks = [
        StreamChunk(text="recovered", done=False),
        StreamChunk(text="", done=True),
    ]
    await inst.process(_make_doll_event("y"))
    second_user = adapter.calls[1]["user"]
    assert "(none — this is the first event)" in second_user
