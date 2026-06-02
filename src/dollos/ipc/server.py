"""WebSocket IPC server."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from dollos.ipc.messages import (
    ErrorMsg,
    ServerMessage,
    TextInput,
    TurnEnd,
    decode_client_message,
    encode_server_message,
)

logger = logging.getLogger(__name__)


Handler = Callable[
    [TextInput, "asyncio.Queue[ServerMessage | None]"], Awaitable[None]
]
"""A handler takes a typed client message and a per-connection sink.

It dispatches work and returns immediately. Output is delivered
asynchronously through the sink, drained by the connection's pump task.
"""


class WebSocketServer:
    """Async WebSocket server.

    Each connection owns a persistent sink Queue and a pump task. Incoming
    client messages are handed to the handler with the sink; the handler
    dispatches and returns. The pump drains the sink to ws.send forever
    until the connection closes. None items in the sink are turn separators
    (skipped), not stream terminators.
    """

    def __init__(
        self,
        host: str,
        port: int,
        handler: Handler,
        *,
        on_connect: (
            Callable[["asyncio.Queue[ServerMessage | None]"], Awaitable[None]]
            | None
        ) = None,
        on_disconnect: (
            Callable[["asyncio.Queue[ServerMessage | None]"], Awaitable[None]]
            | None
        ) = None,
        sink_factory: (
            Callable[[], "asyncio.Queue[ServerMessage | None]"] | None
        ) = None,
    ):
        self._host = host
        self._port_requested = port
        self._handler = handler
        self._server: websockets.asyncio.server.Server | None = None
        self._on_connect_hook = on_connect
        self._on_disconnect_hook = on_disconnect
        self._sink_factory = sink_factory

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
        sink: asyncio.Queue[ServerMessage | None] = (
            self._sink_factory() if self._sink_factory is not None else asyncio.Queue()
        )
        pump_task = asyncio.create_task(self._pump(ws, sink), name="ipc-pump")
        if self._on_connect_hook is not None:
            try:
                await self._on_connect_hook(sink)
            except Exception:
                logger.exception("on_connect hook failed")
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
                await self._handler(msg, sink)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            if self._on_disconnect_hook is not None:
                try:
                    await self._on_disconnect_hook(sink)
                except Exception:
                    logger.exception("on_disconnect hook failed")
            logger.info("client disconnected: %s", ws.remote_address)

    async def _pump(
        self,
        ws: ServerConnection,
        sink: "asyncio.Queue[ServerMessage | None]",
    ) -> None:
        """Drain sink → ws.send forever.

        None items are turn separators (skip and keep going); only
        connection close or task cancellation terminates the pump.
        """
        try:
            while True:
                item = await sink.get()
                if item is None:
                    item = TurnEnd()  # turn separator → signal client
                try:
                    await ws.send(encode_server_message(item))
                except websockets.exceptions.ConnectionClosed:
                    return
                except Exception:
                    logger.exception("pump send failed")
                    return
        except asyncio.CancelledError:
            return

    async def _send_error(self, ws: ServerConnection, message: str) -> None:
        await ws.send(encode_server_message(ErrorMsg(message=message)))
