"""IPC query protocol wire schema (P2 Task 1): QueryState/QueryRecent
(ClientMessage) + QueryResult (ServerMessage)."""
import json

from pydantic import TypeAdapter

from dollos.ipc.messages import (
    ClientMessage,
    QueryRecent,
    QueryResult,
    QueryState,
    ServerMessage,
    decode_client_message,
    encode_server_message,
)


def test_query_state_round_trip():
    m = QueryState(query_id="q1", token="s3cr3t")
    back = decode_client_message(m.model_dump_json())
    assert isinstance(back, QueryState) and back.query_id == "q1" and back.token == "s3cr3t"


def test_query_recent_defaults_n_20():
    m = decode_client_message(json.dumps({"type": "query_recent", "query_id": "q2", "token": "s"}))
    assert isinstance(m, QueryRecent) and m.n == 20


def test_query_result_round_trip():
    m = QueryResult(query_id="q1", ok=True, payload={"mood": "calm"})
    back = TypeAdapter(ServerMessage).validate_json(encode_server_message(m))
    assert isinstance(back, QueryResult) and back.ok is True and back.payload == {"mood": "calm"}
