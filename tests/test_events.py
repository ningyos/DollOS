"""Tests for the two-tier event model (RawEvent / UserTextEvent / DollEvent)."""

import asyncio

from dollos.events import DollEvent, RawEvent, UserTextEvent


def test_user_text_event_holds_text_and_sink():
    sink: asyncio.Queue = asyncio.Queue()
    ev = UserTextEvent(text="hello", response_sink=sink)
    assert ev.text == "hello"
    assert ev.response_sink is sink
    assert isinstance(ev.response_sink, asyncio.Queue)


def test_user_text_event_is_raw_event():
    sink: asyncio.Queue = asyncio.Queue()
    ev = UserTextEvent(text="hi", response_sink=sink)
    assert isinstance(ev, RawEvent)


def test_doll_event_holds_perception_and_raw():
    sink: asyncio.Queue = asyncio.Queue()
    raw = UserTextEvent(text="hi", response_sink=sink)
    de = DollEvent(perception="主人說 hi", raw=raw)
    assert de.perception == "主人說 hi"
    assert de.raw is raw


def test_doll_event_is_not_raw_event():
    sink: asyncio.Queue = asyncio.Queue()
    raw = UserTextEvent(text="hi", response_sink=sink)
    de = DollEvent(perception="x", raw=raw)
    assert not isinstance(de, RawEvent)
