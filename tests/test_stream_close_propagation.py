"""Regression test for the flat_legacy-forever bug found by live smoke
against the real llama.cpp backend (metabolic vital model, 2026-07-10).

Root cause: every real turn exits the streaming consumer loop via `break`
on `chunk.done` (the final SSE event always sets done). The LLM call chain
is a stack of pass-through async generators:

    mind_loop.py _stream_one_pass()      -- wraps ONLY the outermost in
                                             `async with aclosing(stream)`
      -> kernel.py _MindLLMAdapter        -- was a plain
                                             `async for chunk in inner: yield chunk`
        -> composed.py ComposedLLMAdapter -- was a plain
                                             `async for chunk in inner: yield chunk`
          -> transport.py LlamaCppProvider.stream() -- on_usage fires in
                                                        its `finally`

`aclosing()` on the OUTERMOST generator throws `GeneratorExit` into it at
its suspended `yield` when the block exits — but a bare `async for ... yield`
does NOT call `.aclose()` on the iterator it loops over when it unwinds on
GeneratorExit (this is a well-known Python async-generator gotcha, not
specific to this codebase). So the MIDDLE (ComposedLLMAdapter) and INNER
(LlamaCppProvider) generators were left dangling, and `on_usage` only fired
later via asyncio's abandoned-async-generator GC finalizer — strictly AFTER
the energy-drain gate had already read the turn's token accumulators as
still None, so every real turn logged `flat_legacy`.

The fix: each pass-through layer wraps the generator it iterates in its own
`async with aclosing(inner) as s:`, so GeneratorExit propagates synchronously
all the way down the chain the instant the outermost consumer closes it.

This test builds the real multi-layer chain (fake innermost provider ->
real ComposedLLMAdapter -> real _MindLLMAdapter) and proves `on_usage`
fires SYNCHRONOUSLY as part of the `aclosing()` cascade, not later at GC.
Without the fix in composed.py / kernel.py, this test fails (see the
sibling test that flips the flag to demonstrate RED).
"""

from collections.abc import AsyncIterator
from contextlib import aclosing

import pytest

from dollos.kernel import _MindLLMAdapter
from dollos.llm.adapter import StreamChunk
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import PromptTemplate


class _FakeTemplate(PromptTemplate):
    """Minimal template — just needs to render for ComposedLLMAdapter."""

    def render(self, *, system: str, user: str, prefill: str, tools=None) -> str:
        return f"SYS={system}|USR={user}|PRE={prefill}"

    def render_messages(self, *, system: str, messages: list[dict], tools=None) -> str:
        body = "|".join(f"{m['role']}={m['content']}" for m in messages)
        return f"SYS={system}|MSG={body}"


class _FakeInnermostProvider:
    """Mirrors the real LlamaCppProvider.stream() shape: yields a
    done=False chunk then a done=True chunk, and fires `on_usage` in its
    `finally` — exactly like the real transport's SSE loop, where the
    `if data.get("stop"): break` sits AFTER the yield, so the generator is
    still suspended at that yield (not yet at its own finally) when the
    consumer breaks. Only closing this generator (via GeneratorExit at that
    suspended yield) reaches the finally and fires on_usage.
    """

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
        try:
            yield StreamChunk(text="hello", done=False)
            yield StreamChunk(text="", done=True)
            # Real transport: further SSE lines would follow here before
            # its own natural exhaustion. The generator is suspended at the
            # `done=True` yield above until resumed or closed.
        finally:
            if on_usage is not None:
                on_usage(128, 42)


@pytest.mark.asyncio
async def test_composed_adapter_fires_on_usage_synchronously_on_aclose():
    """ComposedLLMAdapter layer alone: break-on-done + aclosing() must fire
    on_usage by the time the `async with aclosing(...)` block exits."""
    calls: list[tuple[int | None, int | None]] = []

    composed = ComposedLLMAdapter(
        provider=_FakeInnermostProvider(), template=_FakeTemplate()
    )

    stream = composed.stream_completion(
        system="s", user="u", on_usage=lambda p, c: calls.append((p, c))
    )
    async with aclosing(stream) as s:
        async for chunk in s:
            if chunk.done:
                break

    # Without the aclosing() wrap in composed.py, the inner (provider)
    # generator is left dangling here — on_usage has NOT fired yet, and
    # this assertion fails.
    assert calls == [(128, 42)]


@pytest.mark.asyncio
async def test_mind_adapter_over_composed_fires_on_usage_synchronously_on_aclose():
    """Full chain: _MindLLMAdapter (kernel.py) wrapping a real
    ComposedLLMAdapter wrapping the fake innermost provider — mirrors the
    real mind_loop.py -> kernel.py -> composed.py -> transport.py stack
    (mind_loop.py's own `aclosing(stream)` around the OUTERMOST generator
    is exercised here directly by the test's `async with aclosing(...)`)."""
    calls: list[tuple[int | None, int | None]] = []

    composed = ComposedLLMAdapter(
        provider=_FakeInnermostProvider(), template=_FakeTemplate()
    )
    mind_adapter = _MindLLMAdapter(composed)

    stream = mind_adapter.stream_completion(
        system="s", user="u", on_usage=lambda p, c: calls.append((p, c))
    )
    async with aclosing(stream) as s:
        async for chunk in s:
            if chunk.done:
                break

    assert calls == [(128, 42)]


@pytest.mark.asyncio
async def test_mind_adapter_stream_messages_fires_on_usage_synchronously_on_aclose():
    """Same chain, but through the stream_messages() path (pass >= 2 of the
    in-turn cascade) rather than stream_completion() (pass 1)."""
    calls: list[tuple[int | None, int | None]] = []

    composed = ComposedLLMAdapter(
        provider=_FakeInnermostProvider(), template=_FakeTemplate()
    )
    mind_adapter = _MindLLMAdapter(composed)

    stream = mind_adapter.stream_messages(
        system="s",
        messages=[{"role": "user", "content": "u1"}],
        on_usage=lambda p, c: calls.append((p, c)),
    )
    async with aclosing(stream) as s:
        async for chunk in s:
            if chunk.done:
                break

    assert calls == [(128, 42)]
