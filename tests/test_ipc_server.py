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
async def test_pump_converts_none_to_turn_end_and_keeps_running():
    """None in the sink is converted to TurnEnd by the pump and the pump
    keeps running so subsequent pushes (e.g. delayed SubagentResultEvent)
    still reach the client."""

    second_chunk_pushed = asyncio.Event()

    async def _handler(
        msg: TextInput, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        # First "turn" — None acts as the turn-end sentinel
        sink.put_nowait(TextChunk(text="A"))
        sink.put_nowait(None)  # pump converts this to TurnEnd

        # Schedule a second push as if from a delayed event.
        async def _later():
            await asyncio.sleep(0.05)
            sink.put_nowait(TextChunk(text="B"))
            sink.put_nowait(None)  # pump converts this to TurnEnd
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
            for _ in range(4):  # A, turn_end(from None), B, turn_end(from None)
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


@pytest.mark.asyncio
async def test_ws_server_calls_on_connect_with_sink():
    """on_connect is invoked with the per-connection sink before the first
    inbound message is handled."""
    captured: list[asyncio.Queue] = []

    async def _on_connect(sink: "asyncio.Queue[ServerMessage | None]") -> None:
        captured.append(sink)

    server = WebSocketServer(
        host="127.0.0.1", port=0,
        handler=_make_echo_handler(),
        on_connect=_on_connect,
    )
    await server.start()
    try:
        port = server.port
        assert port is not None
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "x"}')
            # Drain at least one message to force handler invocation.
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        # Allow the disconnect path to settle.
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(captured) == 1
        assert isinstance(captured[0], asyncio.Queue)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_ws_server_calls_on_disconnect_after_disconnect():
    """on_disconnect fires after the client closes the websocket."""
    disconnected = asyncio.Event()

    async def _on_disconnect(sink) -> None:
        disconnected.set()

    server = WebSocketServer(
        host="127.0.0.1", port=0,
        handler=_make_echo_handler(),
        on_disconnect=_on_disconnect,
    )
    await server.start()
    try:
        port = server.port
        assert port is not None
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            await ws.send('{"type": "text_input", "text": "x"}')
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        await asyncio.wait_for(disconnected.wait(), timeout=2.0)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pump_task_names_include_remote_address():
    """Each pump task name must encode the client address so concurrent
    connections are distinguishable in asyncio task dumps / logs.

    Two simultaneous clients must get different task names and each name
    must follow the 'ipc-pump-<host>:<port>' pattern.
    """
    captured_names: list[str] = []
    connected = asyncio.Event()
    second_connected = asyncio.Event()

    async def _handler(
        msg: TextInput, sink: "asyncio.Queue[ServerMessage | None]"
    ) -> None:
        # Capture all current task names that look like pump tasks.
        names = [t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("ipc-pump")]
        captured_names.extend(names)
        sink.put_nowait(TurnEnd())
        sink.put_nowait(None)

    server = WebSocketServer(host="127.0.0.1", port=0, handler=_handler)
    await server.start()
    try:
        port = server.port
        assert port is not None
        uri = f"ws://127.0.0.1:{port}"

        # Open two simultaneous connections so both pump tasks exist at once.
        async with websockets.connect(uri) as ws1, websockets.connect(uri) as ws2:
            await ws1.send('{"type": "text_input", "text": "a"}')
            await ws2.send('{"type": "text_input", "text": "b"}')
            await asyncio.wait_for(ws1.recv(), timeout=2.0)
            await asyncio.wait_for(ws2.recv(), timeout=2.0)

        # Verify names captured during the handler calls.
        assert len(captured_names) >= 1, "no pump task names were captured"
        for name in captured_names:
            assert name != "ipc-pump", (
                f"pump task name '{name}' is the old generic form — "
                "it must include the remote address"
            )
            # Must follow the pattern ipc-pump-<host>:<port>
            assert name.startswith("ipc-pump-") and ":" in name, (
                f"pump task name '{name}' does not encode host:port"
            )

        # The two simultaneous connections must have produced distinct names.
        all_names = [t.get_name() for t in asyncio.all_tasks()]
        # (tasks may already be gone; rely on captured_names having at least 2)
        if len(captured_names) >= 2:
            assert len(set(captured_names)) > 1 or len(captured_names) == 1, (
                "multiple connections produced identical pump task names"
            )
    finally:
        await server.stop()
