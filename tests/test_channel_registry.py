"""ChannelRegistry — channel locus/kind lookup (spec §3.1)."""
import pytest

from dollos.ipc.channel_registry import ChannelRegistry, ChannelInfo


def test_register_and_get():
    r = ChannelRegistry()
    r.register("disc:123", locus="external", kind="discord")
    assert r.get("disc:123") == ChannelInfo("disc:123", "external", "discord")


def test_locus_of_unknown_defaults_internal():
    r = ChannelRegistry()
    assert r.locus_of("nope") == "internal"     # origin-less = internal
    r.register("disc:1", locus="external", kind="discord")
    assert r.locus_of("disc:1") == "external"


def test_unregister():
    r = ChannelRegistry()
    r.register("c", locus="internal", kind="text")
    r.unregister("c")
    assert r.get("c") is None


def test_register_rejects_bad_locus():
    r = ChannelRegistry()
    with pytest.raises(ValueError):
        r.register("c", locus="sideways", kind="text")
