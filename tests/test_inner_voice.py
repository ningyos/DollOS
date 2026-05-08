"""Tests for InnerVoice.recall() against a fake MemSearch."""

from collections.abc import AsyncIterator

import pytest

from dollos.inner_voice import InnerVoice
from dollos.llm.adapter import LLMAdapter, StreamChunk
from dollos.prompts import PromptRenderer


class _FakeMemSearch:
    """Stub: returns canned hits, captures last query / top_k."""

    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits
        self.last_query: str | None = None
        self.last_top_k: int | None = None
        self.call_count = 0

    async def search(self, query: str, top_k: int = 3) -> list[dict]:
        self.last_query = query
        self.last_top_k = top_k
        self.call_count += 1
        return self._hits


class _FakeLLMAdapter(LLMAdapter):
    """Yield canned chunks. Captures last call args for assertions."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.last_prefill: str | None = None
        self.call_count = 0

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        self.last_system = system
        self.last_user = user
        self.last_prefill = prefill
        self.call_count += 1
        if self._response:
            yield StreamChunk(text=self._response, done=False)
        yield StreamChunk(text="", done=True)


def _make_iv(memsearch, llm, default_top_k: int = 10) -> InnerVoice:
    return InnerVoice(
        memsearch=memsearch,
        llm=llm,
        renderer=PromptRenderer(),
        default_top_k=default_top_k,
    )


@pytest.mark.asyncio
async def test_recall_with_hits_returns_plain_filtered_text():
    mem = _FakeMemSearch(
        hits=[
            {"content": "the sky is blue", "score": 0.9, "source": "shared/2026-05-03.md"},
            {"content": "user likes coffee", "score": 0.8, "source": "shared/2026-05-03.md"},
        ]
    )
    fake_llm = _FakeLLMAdapter(response="- user likes coffee\n- the sky is blue")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("what about coffee")

    # No "RECALL:" prefix — dispatcher does the framing now.
    assert "RECALL:" not in block
    assert not block.startswith("RECALL")
    assert "user likes coffee" in block


@pytest.mark.asyncio
async def test_recall_system_prompt_comes_from_iv_recall_template():
    mem = _FakeMemSearch(hits=[{"content": "a fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- a fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("query")

    assert fake_llm.last_system is not None
    assert "memory recall helper" in fake_llm.last_system
    assert fake_llm.call_count == 1


@pytest.mark.asyncio
async def test_recall_user_block_includes_query_and_candidates():
    mem = _FakeMemSearch(
        hits=[
            {"content": "fact alpha", "score": 0.9, "source": "x.md"},
            {"content": "fact beta", "score": 0.8, "source": "x.md"},
        ]
    )
    fake_llm = _FakeLLMAdapter(response="- fact alpha")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("alpha")

    user_block = fake_llm.last_user
    assert user_block is not None
    assert "Query: alpha" in user_block
    assert "Candidates:" in user_block
    assert "1." in user_block
    assert "2." in user_block
    assert "fact alpha" in user_block
    assert "fact beta" in user_block


@pytest.mark.asyncio
async def test_recall_uses_empty_prefill():
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm)

    await iv.recall("q")

    assert fake_llm.last_prefill == ""


@pytest.mark.asyncio
async def test_recall_empty_hits_returns_empty_string():
    mem = _FakeMemSearch(hits=[])
    fake_llm = _FakeLLMAdapter(response="should not be called")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("anything")

    assert block == ""
    assert fake_llm.call_count == 0


@pytest.mark.asyncio
async def test_recall_strips_whitespace_from_model_output():
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="  \n- fact\n  \n")
    iv = _make_iv(mem, fake_llm)

    block = await iv.recall("q")

    assert block == "- fact"


@pytest.mark.asyncio
async def test_recall_passes_top_k_to_memsearch():
    """default_top_k from settings is used; per-call override also works."""
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm, default_top_k=15)

    await iv.recall("q")
    assert mem.last_top_k == 15

    await iv.recall("q", top_k=3)
    assert mem.last_top_k == 3


@pytest.mark.asyncio
async def test_recall_ignores_character_id_in_step3():
    """character_id is reserved for step 10; step 3 just ignores it."""
    mem = _FakeMemSearch(hits=[{"content": "fact", "score": 0.5, "source": "x.md"}])
    fake_llm = _FakeLLMAdapter(response="- fact")
    iv = _make_iv(mem, fake_llm)

    # Should not raise, should not affect the search call
    await iv.recall("q", character_id="gura")

    assert mem.last_query == "q"
