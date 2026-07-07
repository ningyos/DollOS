"""TurnEndAddressed server message (P1 Task 2) — per-channel turn-end,
symmetric to AddressedText. Only this type is added in P1."""
import json

from pydantic import TypeAdapter

from dollos.ipc.messages import (
    AddressedText,
    ServerMessage,
    TurnEnd,
    TurnEndAddressed,
    encode_server_message,
)


def test_turn_end_addressed_round_trip():
    raw = encode_server_message(TurnEndAddressed(channel_id="mcp:c1:call1"))
    d = json.loads(raw)
    assert d == {"type": "turn_end_addressed", "channel_id": "mcp:c1:call1"}


def test_turn_end_addressed_in_union_unambiguous():
    adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
    msg = adapter.validate_python(
        {"type": "turn_end_addressed", "channel_id": "mcp:c1:call1"}
    )
    assert isinstance(msg, TurnEndAddressed)
    assert msg.channel_id == "mcp:c1:call1"

def test_turn_end_addressed_distinct_from_global_turn_end():
    adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
    assert isinstance(adapter.validate_python({"type": "turn_end"}), TurnEnd)
    assert isinstance(
        adapter.validate_python({"type": "addressed_text",
                                 "channel_id": "x", "text": "y"}),
        AddressedText,
    )
