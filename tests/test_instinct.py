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
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append({"system": system, "user": user, "prefill": prefill})
        for c in self.chunks:
            yield c


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
