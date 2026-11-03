"""Terminal front end.

    you> text                 the owner talks
    you> /dm <who> <text>     someone else DMs Doll
    you> /approve <job>       run a job Doll asked for
    you> /deny <job> [why]    refuse it
    you> /pending             list jobs waiting for approval
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


def _ask(job_id: str, tool: str, args: dict, line_id: str | None) -> None:
    line = f" (line {line_id})" if line_id else ""
    print(f"\n[approval] {job_id}{line} {tool}: {args.get('command')}\n"
          f"           /approve {job_id}   or   /deny {job_id} [why]\n", flush=True)


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
            if line == "/pending":
                print("\n".join(f"{j}: {c}" for j, c in doll.pending()) or "(none)", flush=True)
                continue
            if line.startswith("/approve "):
                print(doll.approve(line.split()[1]), flush=True)
                continue
            if line.startswith("/deny "):
                parts = line.split(" ", 2)
                print(doll.deny(parts[1], parts[2] if len(parts) > 2 else ""), flush=True)
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
    doll = Doll(cfg, Path.cwd(), _say, _ask)

    async def go() -> None:
        await doll.boot()
        print(f"{doll.name} is up. /dm <who> <text>, /approve <job>, /deny <job>, /pending, "
              "/lines, /quit", flush=True)
        await _repl(doll)

    asyncio.run(go())


if __name__ == "__main__":
    main()
