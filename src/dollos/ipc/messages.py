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


class ChannelRegister(BaseModel):
    """A bridge registers an external channel with the daemon (spec §3.1).

    Wire-schema only in P1a — kernel wiring of ChannelRegister into
    ChannelRegistry/SinkResolver registration is P1b.
    """
    type: Literal["channel_register"] = "channel_register"
    channel_id: str
    locus: str
    kind: str


class ChannelEvent(BaseModel):
    """An inbound event on a registered external channel (spec §3.1).

    Wire-schema only in P1a — kernel wiring of ChannelEvent→Perception is P1b.
    """
    type: Literal["channel_event"] = "channel_event"
    channel_id: str
    payload: dict


class QueryState(BaseModel):
    """Debug-only read query: snapshot Doll's self-state. Carries a REQUIRED
    daemon token — the daemon fail-closes any query whose token ≠ settings.mcp.query_token
    (the IPC server has no connection auth; see spec §C.3 R-DECISION-4)."""
    type: Literal["query_state"] = "query_state"
    query_id: str
    token: str


class QueryRecent(BaseModel):
    """Debug-only read query: recent EXTERNAL_PUBLIC-origin interactions (n clamped
    by the daemon). REQUIRED daemon token, same fail-closed rule as QueryState."""
    type: Literal["query_recent"] = "query_recent"
    query_id: str
    token: str
    n: int = 20


ClientMessage = Annotated[
    TextInput
    | Interrupt
    | WebRTCOfferIn
    | ICECandidateIn
    | UtteranceStart
    | UtteranceEnd
    | ChannelRegister
    | ChannelEvent
    | QueryState
    | QueryRecent,
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


class AddressedText(BaseModel):
    """Streamed sentence for an external-origin turn, addressed to its
    channel_id (spec §3.1) — so the bridge knows where to route it. Internal
    (origin-less) turns keep emitting plain TextChunk, unchanged."""
    type: Literal["addressed_text"] = "addressed_text"
    channel_id: str
    text: str


class TurnEndAddressed(BaseModel):
    """End-of-turn marker for an external-origin stream, carrying channel_id
    so a multiplexing connector knows WHICH channel finished (symmetric to
    AddressedText). Internal (origin-less) turns keep emitting the plain
    global TurnEnd, unchanged."""
    type: Literal["turn_end_addressed"] = "turn_end_addressed"
    channel_id: str


class QueryResult(BaseModel):
    """Response to a QueryState/QueryRecent, correlated by query_id. ok=false means
    the token was missing/wrong or the query surface is disabled — payload is empty,
    no data returned (fail-closed)."""
    type: Literal["query_result"] = "query_result"
    query_id: str
    ok: bool
    payload: dict


ServerMessage = Annotated[
    TextChunk | TurnEnd | ErrorMsg | SayAborted | WebRTCAnswerOut | ICECandidateOut
    | AddressedText | TurnEndAddressed | QueryResult,
    Field(discriminator="type"),
]


def encode_server_message(msg: ServerMessage) -> str:
    """Serialize a server message to JSON."""
    return msg.model_dump_json()
