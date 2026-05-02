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
