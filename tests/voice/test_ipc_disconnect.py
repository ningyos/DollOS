"""IPC server: on_disconnect hook now receives the sink."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from dollos.ipc.server import WebSocketServer
from dollos.ipc.messages import TextInput


@pytest.mark.asyncio
async def test_on_disconnect_receives_sink():
    captured_sink_on_connect = None
    captured_sink_on_disconnect = None

    async def on_connect(sink):
        nonlocal captured_sink_on_connect
        captured_sink_on_connect = sink

    async def on_disconnect(sink):
        nonlocal captured_sink_on_disconnect
        captured_sink_on_disconnect = sink

    async def handler(msg, sink):
        pass

    server = WebSocketServer(
        host="127.0.0.1", port=0, handler=handler,
        on_connect=on_connect, on_disconnect=on_disconnect,
    )
    await server.start()
    try:
        port = server.port
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            pass  # connect then disconnect immediately
        # Give the server a tick to run the disconnect hook.
        for _ in range(20):
            if captured_sink_on_disconnect is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert captured_sink_on_connect is not None
    assert captured_sink_on_disconnect is captured_sink_on_connect
