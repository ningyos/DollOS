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


def test_decode_existing_text_input_still_works():
    """Adding Interrupt to the union must not break TextInput decoding."""
    msg = decode_client_message('{"type": "text_input", "text": "hi"}')
    assert isinstance(msg, TextInput)
    assert msg.text == "hi"


def test_say_aborted_serializes_with_default_reason():
    m = SayAborted()
    d = m.model_dump()
    assert d["type"] == "say_aborted"
    assert d["reason"] == "user_interrupted"


def test_say_aborted_custom_reason():
    m = SayAborted(reason="external_command")
    assert m.model_dump()["reason"] == "external_command"


def test_say_aborted_is_in_server_message_union():
    """SayAborted should be a valid ServerMessage variant (typed via Pydantic)."""
    from dollos.ipc.messages import ServerMessage
    msg: ServerMessage = SayAborted()  # type: ignore[assignment]
    assert msg.type == "say_aborted"
