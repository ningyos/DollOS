"""A stdio MCP server for tests/test_harness_mcp_client.py (plan 2026-10-27
Task 7). Launched as ``[sys.executable, this file, *flags]``:

- ``echo(text, repeat=1)``: the text back, ``repeat`` times on separate lines.
- ``fail(message)``: an ``isError`` result carrying ``message``.
- ``crash()``: the process exits at once, mid-call.
- ``--announce-change``: after each echo, send ``tools/list_changed``.
- ``--pidfile PATH``: write this process's pid there at start.
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("fake")
ANNOUNCE = "--announce-change" in sys.argv


@mcp.tool(description="Echo the text back.")
async def echo(text: str, ctx: Context, repeat: int = 1) -> str:
    if ANNOUNCE:
        await ctx.session.send_tool_list_changed()
    return "\n".join([text] * repeat)


@mcp.tool(description="Always fails with the message.")
def fail(message: str) -> str:
    raise ValueError(message)


@mcp.tool(description="Exit the server process mid-call.")
def crash() -> str:
    os._exit(3)


if __name__ == "__main__":
    if "--pidfile" in sys.argv:
        path = sys.argv[sys.argv.index("--pidfile") + 1]
        with open(path, "w") as f:
            f.write(str(os.getpid()))
    mcp.run("stdio")
