"""Terminal front end.

    you> text                 the owner talks
    you> /dm <who> <text>     someone else DMs Doll
    you> /lines               show work lines and jobs
    you> /quit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import config
from .core import Doll


def _say(audience: str, text: str) -> None:
    tag = "" if audience == "private" else f" → {audience[3:]}"
    print(f"\n[Doll{tag}] {text}\n", flush=True)


async def _repl(doll: Doll) -> None:
    loop = asyncio.get_running_loop()
    runner = asyncio.create_task(doll.run())
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/lines":
                print(doll._lines_block(), flush=True)
                continue
            if line.startswith("/dm "):
                _, who, text = line.split(" ", 2)
                doll.dm(who, text)
                continue
            doll.owner_said(line)
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="dollcore")
    ap.add_argument("--config", type=Path, required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(args.config)
    doll = Doll(cfg, Path.cwd(), _say)

    async def go() -> None:
        await doll.boot()
        print(f"{doll.name} is up. /dm <who> <text>, /lines, /quit", flush=True)
        await _repl(doll)

    asyncio.run(go())


if __name__ == "__main__":
    main()
