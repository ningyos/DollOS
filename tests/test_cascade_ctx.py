import asyncio
import pytest


def test_cascade_ctx_starts_uncancelled():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    assert not ctx.cancelled


def test_cascade_ctx_cancel_sets_flag():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    ctx.cancel()
    assert ctx.cancelled


@pytest.mark.asyncio
async def test_cascade_ctx_wait_cancelled_blocks_until_cancel():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    waiter = asyncio.create_task(ctx.wait_cancelled())
    await asyncio.sleep(0.02)
    assert not waiter.done()
    ctx.cancel()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done()
