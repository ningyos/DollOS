"""WebSocket IPC server."""

import logging
from collections.abc import AsyncIterator, Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from dollos.ipc.messages import (
    ErrorMsg,
    ServerMessage,
    TextInput,
    decode_client_message,
    encode_server_message,
)

logger = logging.getLogger(__name__)


Handler = Callable[[TextInput], AsyncIterator[ServerMessage]]
"""A handler takes a typed client message and yields server messages."""


class WebSocketServer:
    """Async WebSocket server.

    Each incoming client message is dispatched to the handler callback. The
    handler is expected to yield a stream of ServerMessage objects.
    """

    def __init__(self, host: str, port: int, handler: Handler):
        self._host = host
        self._port_requested = port
        self._handler = handler
        self._server: websockets.asyncio.server.Server | None = None

    @property
    def port(self) -> int | None:
        if self._server is None:
            return None
        for sock in self._server.sockets:
            return sock.getsockname()[1]
        return None

    async def start(self) -> None:
        self._server = await serve(self._on_connect, self._host, self._port_requested)
        logger.info("WebSocket server listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_connect(self, ws: ServerConnection) -> None:
        logger.info("client connected: %s", ws.remote_address)
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    await self._send_error(ws, "binary frames not supported in v1")
                    continue
                try:
                    msg = decode_client_message(raw)
                except ValueError as e:
                    await self._send_error(ws, f"decode error: {e}")
                    continue

                async for out in self._handler(msg):
                    await ws.send(encode_server_message(out))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            logger.info("client disconnected: %s", ws.remote_address)

    async def _send_error(self, ws: ServerConnection, message: str) -> None:
        await ws.send(encode_server_message(ErrorMsg(message=message)))
