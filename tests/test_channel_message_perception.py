"""ChannelMessage perception kind + minimal rendering (spec §3.2)."""
import time

from dollos.mind.mind_state import Perception
from dollos.mind.mind_prompt import _percep_body


def _cm(**data):
    base = dict(channel_id="disc:g1:c1", guild="g1", channel="general",
                author="stranger", author_id="42", content="hello there",
                mentioned=False, is_dm=False, msg_id="m1", author_is_owner=False)
    base.update(data)
    return Perception(kind="ChannelMessage", t=time.time(), data=base)


def test_channel_message_is_valid_kind():
    p = _cm()
    assert p.kind == "ChannelMessage"          # no Literal ValidationError


def test_percep_body_renders_content_and_author():
    body = _percep_body(_cm(author="alice", content="ping"))
    assert "alice" in body and "ping" in body and "general" in body


def test_percep_body_dm_marks_dm():
    body = _percep_body(_cm(is_dm=True, channel="DM", content="hi"))
    assert "私訊" in body or "DM" in body
