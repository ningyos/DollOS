def test_cascade_ctx_starts_uncancelled():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    assert not ctx.cancelled


def test_cascade_ctx_cancel_sets_flag():
    from dollos.cascade.cascade_ctx import CascadeCtx
    ctx = CascadeCtx()
    ctx.cancel()
    assert ctx.cancelled
