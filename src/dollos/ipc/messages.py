"""IPC message schemas (pydantic).

Wire format: JSON for control messages (`type` field discriminator).
Binary frames (audio etc.) come in later plans.
"""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


# ===== Client → Server =====

class TextInput(BaseModel):
    type: Literal["text_input"] = "text_input"
    text: str


ClientMessage = Annotated[TextInput, Field(discriminator="type")]
_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def decode_client_message(raw: str) -> ClientMessage:
    """Parse a raw JSON string into a typed client message.

    Raises ValueError on malformed JSON or unknown message type.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON: {e}") from e
    return _client_adapter.validate_python(data)


# ===== Server → Client =====

class TextChunk(BaseModel):
    type: Literal["text_chunk"] = "text_chunk"
    text: str


class TurnEnd(BaseModel):
    type: Literal["turn_end"] = "turn_end"


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Annotated[
    TextChunk | TurnEnd | ErrorMsg,
    Field(discriminator="type"),
]


def encode_server_message(msg: ServerMessage) -> str:
    """Serialize a server message to JSON."""
    return msg.model_dump_json()
