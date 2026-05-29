"""Dummy DollOS daemon for the Tauri spike.

Run with:
    cd experiments/tauri-spike
    uv run --with websockets python dummy_daemon.py

Listens on ws://localhost:9876. Every 5s pushes a `hello` event to each
connected client. Echoes anything the client sends back as `echo`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.asyncio.server import ServerConnection, serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dummy-daemon")


async def push_hello_loop(ws: ServerConnection) -> None:
    """Push a `hello` event every 5s for the life of the connection."""
    counter = 0
    try:
        while True:
            await asyncio.sleep(5)
            counter += 1
            msg = {"type": "hello", "text": f"Doll says hi (#{counter})"}
            await ws.send(json.dumps(msg))
            log.info("→ hello #%d to %s", counter, ws.remote_address)
    except websockets.ConnectionClosed:
        return


async def echo_loop(ws: ServerConnection) -> None:
    """Echo every received message wrapped in {type: 'echo', original}."""
    try:
        async for raw in ws:
            try:
                original = json.loads(raw)
            except json.JSONDecodeError:
                original = raw  # echo back as string
            log.info("← from %s: %r", ws.remote_address, original)
            await ws.send(json.dumps({"type": "echo", "original": original}))
    except websockets.ConnectionClosed:
        return


async def handler(ws: ServerConnection) -> None:
    log.info("client connected: %s", ws.remote_address)
    # initial greeting so the UI sees something immediately
    await ws.send(json.dumps({"type": "hello", "text": "Doll says hi (connect)"}))
    pusher = asyncio.create_task(push_hello_loop(ws))
    echoer = asyncio.create_task(echo_loop(ws))
    done, pending = await asyncio.wait(
        [pusher, echoer], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    log.info("client gone: %s", ws.remote_address)


async def main() -> None:
    async with serve(handler, "localhost", 9876):
        log.info("dummy daemon listening on ws://localhost:9876")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
