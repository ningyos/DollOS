"""Minimal WS client for DollOS IPC smoke testing.

Usage:
    uv run python experiments/ws_client.py "你好"
    uv run python experiments/ws_client.py --interactive

Requires DollOS daemon running (default ws://127.0.0.1:9876).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def one_turn(uri: str, text: str) -> None:
    print(f"\n>>> {text}", flush=True)
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "text_input", "text": text}))
        sys.stdout.write("<<< ")
        sys.stdout.flush()
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "text_chunk":
                sys.stdout.write(msg["text"])
                sys.stdout.flush()
            elif t == "turn_end":
                sys.stdout.write("\n[turn_end]\n")
                sys.stdout.flush()
                return
            elif t == "error":
                sys.stdout.write(f"\n[error: {msg['message']}]\n")
                sys.stdout.flush()
                return
            else:
                sys.stdout.write(f"\n[unknown msg type {t}: {msg}]\n")
                sys.stdout.flush()


async def interactive(uri: str) -> None:
    print(f"connected to {uri}; Ctrl-D / empty line to quit", flush=True)
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            print()
            return
        if not line:
            return
        await one_turn(uri, line)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="?", help="single message; omit for interactive mode")
    p.add_argument("--uri", default="ws://127.0.0.1:9876")
    p.add_argument("--interactive", "-i", action="store_true")
    args = p.parse_args()

    if args.interactive or args.text is None:
        asyncio.run(interactive(args.uri))
    else:
        asyncio.run(one_turn(args.uri, args.text))


if __name__ == "__main__":
    main()
