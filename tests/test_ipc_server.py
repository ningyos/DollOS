"""Tests for WebSocket server."""

import asyncio

import pytest
import websockets

from dollos.ipc.messages import ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer


def _make_echo_handler():
    """Test handler — pushes input chunked + TurnEnd + None to the sink."""

    async def _handler(
        msg: TextInput, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        for ch in msg.text:
            sink.put_nowait(TextChunk(text=ch))
        sink.put_nowait(TurnEnd())
        sink.put_nowait(None)  # turn separator (pump should skip)

    return _handler


@pytest.mark.asyncio
async def test_server_accepts_text_input_and_streams_back():
    server = WebSocketServer(
        host="127.0.0.1", port=0, handler=_make_echo_handler()
    )
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
    server = WebSocketServer(
        host="127.0.0.1", port=0, handler=_make_echo_handler()
    )
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


@pytest.mark.asyncio
async def test_pump_treats_none_as_turn_separator_not_termination():
    """After the handler pushes None (turn separator), the pump must keep
    running so a subsequent push (e.g. from a delayed event like a
    SubagentResultEvent) still reaches the client."""

    second_chunk_pushed = asyncio.Event()

    async def _handler(
        msg: TextInput, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        # First "turn"
        sink.put_nowait(TextChunk(text="A"))
        sink.put_nowait(TurnEnd())
        sink.put_nowait(None)  # separator

        # Schedule a second push as if from a delayed event.
        async def _later():
            await asyncio.sleep(0.05)
            sink.put_nowait(TextChunk(text="B"))
            sink.put_nowait(TurnEnd())
            sink.put_nowait(None)
            second_chunk_pushed.set()

        asyncio.create_task(_later())

    server = WebSocketServer(host="127.0.0.1", port=0, handler=_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "x"}')
            msgs = []
            for _ in range(4):  # A, turn_end, B, turn_end
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msgs.append(raw)
            await asyncio.wait_for(second_chunk_pushed.wait(), timeout=1.0)

        assert '"A"' in msgs[0]
        assert '"turn_end"' in msgs[1]
        assert '"B"' in msgs[2]
        assert '"turn_end"' in msgs[3]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pump_forwards_items_from_sink_to_ws_send():
    """A handler can push directly into the sink; pump forwards each item
    as an encoded ServerMessage frame."""

    async def _handler(
        msg: TextInput, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        sink.put_nowait(TextChunk(text="hello"))
        sink.put_nowait(TurnEnd())
        sink.put_nowait(None)

    server = WebSocketServer(host="127.0.0.1", port=0, handler=_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "x"}')
            raw1 = await asyncio.wait_for(ws.recv(), timeout=2.0)
            raw2 = await asyncio.wait_for(ws.recv(), timeout=2.0)
        assert '"text_chunk"' in raw1 and '"hello"' in raw1
        assert '"turn_end"' in raw2
    finally:
        await server.stop()
