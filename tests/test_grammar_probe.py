"""Startup probe: fail-closed check that the llama-server backend supports
GBNF bounded repetition `{m,n}` (spec §11 / R1-7). The think line-length
bound (Task 1) depends on `[^\n]{1,64}`; an old llama.cpp rejects `{m,n}`
grammars per-request (HTTP error on every turn), not at Python build time —
no-fallback can't catch that. This probe runs once at startup so a
too-old server fails the daemon boot instead of every conversation turn.
"""

import pytest

from dollos.llm.grammar_probe import assert_bounded_repetition_supported


class _OkLLM:
    """Mimics a capable server: streams a chunk, then a done chunk."""

    async def stream_completion(self, **kw):
        class _C:
            text = "a"
            done = False

        class _D:
            text = ""
            done = True

        yield _C()
        yield _D()


class _RejectLLM:
    """Mimics a too-old llama-server rejecting `{m,n}` grammar syntax."""

    async def stream_completion(self, **kw):
        raise RuntimeError("grammar parse error: unexpected '{'")
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_probe_passes_on_capable_server():
    await assert_bounded_repetition_supported(_OkLLM())  # must not raise


@pytest.mark.asyncio
async def test_probe_fails_closed_on_old_server():
    with pytest.raises(RuntimeError, match="bounded repetition"):
        await assert_bounded_repetition_supported(_RejectLLM())
