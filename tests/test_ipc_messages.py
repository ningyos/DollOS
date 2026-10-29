"""Tests for IPC message schemas."""

import json

import pytest
from pydantic import ValidationError

from dollos.ipc.messages import (
    TextChunk,
    TextInput,
    TurnEnd,
    decode_client_message,
    encode_server_message,
)


def test_text_input_round_trip():
    msg = TextInput(text="hello")
    raw = msg.model_dump_json()
    parsed = json.loads(raw)
    assert parsed == {"type": "text_input", "text": "hello"}


def test_decode_client_message_text_input():
    raw = '{"type": "text_input", "text": "hi"}'
    msg = decode_client_message(raw)
    assert isinstance(msg, TextInput)
    assert msg.text == "hi"


def test_decode_client_message_unknown_type_raises():
    raw = '{"type": "unknown_type"}'
    with pytest.raises(ValidationError):
        decode_client_message(raw)


def test_decode_client_message_malformed_json_raises():
    with pytest.raises(ValueError):
        decode_client_message("not json")


def test_encode_text_chunk():
    msg = TextChunk(text="world")
    raw = encode_server_message(msg)
    parsed = json.loads(raw)
    assert parsed == {"type": "text_chunk", "text": "world"}


def test_encode_turn_end():
    msg = TurnEnd()
    raw = encode_server_message(msg)
    parsed = json.loads(raw)
    assert parsed == {"type": "turn_end"}


from dollos.ipc.messages import Interrupt, SayAborted


def test_decode_interrupt():
    msg = decode_client_message('{"type": "interrupt"}')
    assert isinstance(msg, Interrupt)


def test_say_aborted_serializes_with_default_reason():
    m = SayAborted()
    d = m.model_dump()
    assert d["type"] == "say_aborted"
    assert d["reason"] == "user_interrupted"


# --- AddressedText / ChannelRegister / ChannelEvent (P1a Task 5, spec §3.1) ---


def test_decode_channel_register():
    from dollos.ipc.messages import ChannelRegister
    raw = '{"type": "channel_register", "channel_id": "disc:1", "locus": "external", "kind": "discord"}'
    msg = decode_client_message(raw)
    assert isinstance(msg, ChannelRegister)
    assert msg.channel_id == "disc:1"
    assert msg.locus == "external"
    assert msg.kind == "discord"


def test_decode_channel_event():
    from dollos.ipc.messages import ChannelEvent
    raw = '{"type": "channel_event", "channel_id": "disc:1", "payload": {"text": "hi"}}'
    msg = decode_client_message(raw)
    assert isinstance(msg, ChannelEvent)
    assert msg.channel_id == "disc:1"
    assert msg.payload == {"text": "hi"}
