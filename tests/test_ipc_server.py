"""Tests for WebSocket server."""

import asyncio
from collections.abc import AsyncIterator

import pytest
import websockets

from dollos.ipc.messages import ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer


async def _echo_handler(msg: TextInput) -> AsyncIterator[ServerMessage]:
    """Test handler — yields the input text chunked, then turn_end."""
    for ch in msg.text:
        yield TextChunk(text=ch)
    yield TurnEnd()


@pytest.mark.asyncio
async def test_server_accepts_text_input_and_streams_back():
    server = WebSocketServer(host="127.0.0.1", port=0, handler=_echo_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None

        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "hi"}')
            msgs = []
            for _ in range(3):  # "h", "i", turn_end
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msgs.append(raw)

        assert '"text_chunk"' in msgs[0] and '"h"' in msgs[0]
        assert '"text_chunk"' in msgs[1] and '"i"' in msgs[1]
        assert '"turn_end"' in msgs[2]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_sends_error_on_malformed_message():
    server = WebSocketServer(host="127.0.0.1", port=0, handler=_echo_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None

        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send("not json")
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)

        assert '"error"' in raw
    finally:
        await server.stop()
