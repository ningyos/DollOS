import asyncio
import pytest

from dollos.mind.sink_resolver import SinkResolver, DummySink


@pytest.mark.asyncio
async def test_no_sink_returns_dummy() -> None:
    resolver = SinkResolver()
    sink = resolver()
    assert isinstance(sink, DummySink)


@pytest.mark.asyncio
async def test_register_then_resolve_returns_sink() -> None:
    resolver = SinkResolver()
    real_sink = asyncio.Queue()
    handle = resolver.register(real_sink)
    sink = resolver()
    assert sink is real_sink
    resolver.unregister(handle)
    assert isinstance(resolver(), DummySink)


@pytest.mark.asyncio
async def test_most_recent_wins() -> None:
    resolver = SinkResolver()
    sink_a = asyncio.Queue()
    sink_b = asyncio.Queue()
    resolver.register(sink_a)
    resolver.register(sink_b)
    assert resolver() is sink_b


@pytest.mark.asyncio
async def test_dummy_sink_drops_messages_silently() -> None:
    dummy = DummySink()
    # put_nowait on dummy should not raise
    dummy.put_nowait({"type": "text_chunk", "text": "hi"})


# --- C1 regression: disconnect-out-of-order must not corrupt surviving handle ---


def test_disconnect_out_of_order_routes_to_surviving_sink() -> None:
    """C1 regression: A (handle 0) and B (handle 1) are both registered.
    A disconnects first.  __call__() must still route to B — not to the dummy
    and not to A's dead queue.

    With the old list+pop(index) scheme, pop(0) shifted B from index 1→0 but
    the kernel still held B→1; B's later unregister(1) was a silent no-op
    (out-of-bounds), leaving B's dead queue as the permanent top of the stack.
    With the new dict+monotonic-handle scheme this cannot happen.
    """

    class _RecordSink:
        def __init__(self, name: str) -> None:
            self.name = name
            self.items: list = []

        def put_nowait(self, item) -> None:
            self.items.append(item)

    resolver = SinkResolver()
    sink_a = _RecordSink("A")
    sink_b = _RecordSink("B")

    handle_a = resolver.register(sink_a)
    handle_b = resolver.register(sink_b)  # noqa: F841 — stored for completeness

    # A disconnects first (the bug trigger)
    resolver.unregister(handle_a)

    # __call__ must resolve to B (most-recently registered surviving sink)
    active = resolver()
    assert active is sink_b, (
        f"expected B but got {active!r}; "
        "unregistering A must not corrupt B's handle"
    )

    active.put_nowait("hello")
    assert sink_b.items == ["hello"]
    assert sink_a.items == [], "A must receive nothing after unregister"


def test_unregister_all_returns_dummy() -> None:
    """After all sinks unregister __call__ falls back to the DummySink."""
    resolver = SinkResolver()
    q = asyncio.Queue()
    h = resolver.register(q)
    resolver.unregister(h)
    assert isinstance(resolver(), DummySink)


def test_handles_are_stable_across_multiple_registers_and_unregisters() -> None:
    """Monotonic handles remain valid regardless of how many intermediate
    registrations / unregistrations happen between register and unregister."""
    resolver = SinkResolver()
    items_a: list = []
    items_c: list = []

    class _S:
        def __init__(self, bucket): self._b = bucket
        def put_nowait(self, v): self._b.append(v)

    h_a = resolver.register(_S(items_a))
    h_b = resolver.register(_S([]))
    resolver.unregister(h_b)          # B gone — shifts nothing in new scheme
    h_c = resolver.register(_S(items_c))

    resolver.unregister(h_a)          # A gone — C is now most-recent

    resolver().put_nowait("x")
    assert items_c == ["x"]
    assert items_a == []
