"""SinkResolver locus/origin routing (spec §3.1 R1-arch I2)."""
from dollos.mind.sink_resolver import SinkResolver, DummySink


class _Sink:
    def __init__(self): self.items = []
    def put_nowait(self, item): self.items.append(item)


def test_backcompat_register_no_kwargs_is_internal():
    r = SinkResolver()
    s = _Sink()
    r.register(s)                       # old signature
    assert r() is s                     # origin-less → internal sink


def test_external_sink_never_steals_internal_output():
    r = SinkResolver()
    internal = _Sink(); external = _Sink()
    r.register(internal, locus="internal")
    r.register(external, locus="external", channel_id="disc:1")   # higher handle
    # origin-less internal turn must resolve to internal, NOT the newer external
    assert r() is internal
    assert r(None) is internal


def test_origin_routes_to_matching_external_sink():
    r = SinkResolver()
    internal = _Sink(); ext_a = _Sink(); ext_b = _Sink()
    r.register(internal, locus="internal")
    r.register(ext_a, locus="external", channel_id="A")
    r.register(ext_b, locus="external", channel_id="B")
    assert r("A") is ext_a
    assert r("B") is ext_b


def test_unknown_origin_falls_to_internal():
    r = SinkResolver()
    internal = _Sink()
    r.register(internal, locus="internal")
    r.register(_Sink(), locus="external", channel_id="A")
    assert r("ZZZ") is internal          # unknown origin → internal, not external


def test_empty_returns_dummy():
    r = SinkResolver()
    assert isinstance(r(), DummySink)
