"""P1e Task 6: ChannelMessage rendering surfaces owner/stranger identity
(spec §3.4, plan 4-way split table row "rendered prompt 身分標示"). Without
this, Doll has no in-prompt signal for who she's talking to — she can't
judge whether/how to respond to a stranger vs the owner. Descriptive
narration only (no behavioral command) — P1d will refine further.
"""
from __future__ import annotations

import time

from dollos.mind.mind_state import Perception
from dollos.mind.mind_prompt import _percep_body


def _cm(**data):
    base = dict(channel_id="disc:g1:c1", guild="g1", channel="general",
                author="stranger", author_id="42", content="hello there",
                mentioned=False, is_dm=False, msg_id="m1", author_is_owner=False)
    base.update(data)
    return Perception(kind="ChannelMessage", t=time.time(), data=base)


def test_owner_dm_renders_owner_marker():
    body = _percep_body(_cm(author_is_owner=True, is_dm=True, content="幫我看一下"))
    assert body == "[Owner, DM] Owner: 幫我看一下"


def test_owner_in_channel_renders_owner_marker_and_channel():
    body = _percep_body(_cm(author_is_owner=True, channel="general", content="hi"))
    assert body == "[Owner, #general] Owner: hi"


def test_stranger_renders_stranger_marker_author_and_channel():
    body = _percep_body(_cm(author_is_owner=False, author="alice",
                             channel="general", content="hi there"))
    assert body == "[#general] stranger alice: hi there"


def test_owner_and_stranger_render_differently():
    """Teeth: the two branches must not collapse to the same text — if the
    author_is_owner check were dropped, this would fail."""
    owner_body = _percep_body(_cm(author_is_owner=True, content="same content"))
    stranger_body = _percep_body(_cm(author_is_owner=False, content="same content"))
    assert owner_body != stranger_body
    assert "Owner" not in stranger_body
    assert "stranger" not in owner_body


def test_stranger_dm_still_marks_stranger_not_owner():
    """A non-owner DM'ing directly (is_dm=True, author_is_owner=False) must
    still read as a stranger — author_is_owner drives the identity marker,
    not the DM/channel distinction."""
    body = _percep_body(_cm(is_dm=True, channel="DM", author_is_owner=False,
                             author="rando", content="hi"))
    assert body == "[DM] stranger rando: hi"
