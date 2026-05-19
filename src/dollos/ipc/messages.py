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


class WebRTCOfferIn(BaseModel):
    type: Literal["webrtc_offer"] = "webrtc_offer"
    sdp: str


class ICECandidateIn(BaseModel):
    type: Literal["ice_candidate"] = "ice_candidate"
    candidate: str
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None


class UtteranceStart(BaseModel):
    type: Literal["utterance_start"] = "utterance_start"
    sample_rate: int


class UtteranceEnd(BaseModel):
    type: Literal["utterance_end"] = "utterance_end"


class Interrupt(BaseModel):
    """User explicitly requests cancellation (without new TextInput).

    Also implicitly triggered by sending a new TextInput while a cascade
    is active — see kernel._handle_message.
    """
    type: Literal["interrupt"] = "interrupt"


ClientMessage = Annotated[
    TextInput | Interrupt | WebRTCOfferIn | ICECandidateIn | UtteranceStart | UtteranceEnd,
    Field(discriminator="type"),
]
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


# ===== Server → Client (additions) =====

class WebRTCAnswerOut(BaseModel):
    type: Literal["webrtc_answer"] = "webrtc_answer"
    sdp: str


class ICECandidateOut(BaseModel):
    type: Literal["ice_candidate"] = "ice_candidate"
    candidate: str
    sdpMid: str | None = None
    sdpMLineIndex: int | None = None


class SayAborted(BaseModel):
    """Server tells client the active TTS stream was cancelled.

    Sent when a cascade is preempted by a new TextInput or explicit Interrupt.
    Client should clear any visual cues / audio buffer indicators.
    """
    type: Literal["say_aborted"] = "say_aborted"
    reason: str = "user_interrupted"


ServerMessage = Annotated[
    TextChunk | TurnEnd | ErrorMsg | SayAborted | WebRTCAnswerOut | ICECandidateOut,
    Field(discriminator="type"),
]


def encode_server_message(msg: ServerMessage) -> str:
    """Serialize a server message to JSON."""
    return msg.model_dump_json()
