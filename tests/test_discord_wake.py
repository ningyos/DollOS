"""L0 wake rules + self-filter (spec §3.3/§3.4 L0 + C3)."""
from dollos.discord_bridge.wake import l0_wake


def _e(**kw):
    base = dict(author_id="42", is_dm=False, mentioned=False,
                content="just chatting", channel_id="c1")
    base.update(kw)
    return base


def _cfg(**kw):
    base = dict(bot_id="bot", owner_id="owner", name_aliases=["gura", "古拉"],
                always_wake_channels=set())
    base.update(kw)
    return base


def test_self_message_never_wakes():
    assert l0_wake(_e(author_id="bot", content="gura here"), **_cfg()) is False


def test_dm_wakes():
    assert l0_wake(_e(is_dm=True), **_cfg()) is True


def test_mention_wakes():
    assert l0_wake(_e(mentioned=True), **_cfg()) is True


def test_name_alias_substring_wakes():
    assert l0_wake(_e(content="hey 古拉 look"), **_cfg()) is True


def test_unrelated_public_chatter_does_not_wake():
    assert l0_wake(_e(content="anyone up for a game"), **_cfg()) is False


def test_always_wake_channel():
    assert l0_wake(_e(channel_id="vip"), **_cfg(always_wake_channels={"vip"})) is True
