"""dollos-mcp CLI.

Usage:
    python -m dollos.mcp_server --daemon ws://127.0.0.1:9876 \\
        --config mcp.toml --data-root data

Runs TWO roles in one process:
  1. outward: a FastMCP streamable-HTTP server bound loopback (mcp.toml
     [server] bind_host/bind_port). bind_host is fail-closed validated to a
     loopback literal (spec §E) — any other value raises and refuses to
     start. Exposes the peer tool talk(name, message).
  2. inward: a daemon IPC WS client (reconnecting), feeding inbound frames
     to DaemonLink.dispatch and sending ChannelRegister/ChannelEvent out.

Thin untested wiring on purpose — all IPC-mapping logic lives in
DaemonLink (tests/test_mcp_daemon_link.py). Mirrors
discord_bridge/__main__.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
import uuid
from pathlib import Path

import websockets
from mcp.server.fastmcp import Context, FastMCP

from dollos.mcp_server.daemon_link import DaemonLink

logger = logging.getLogger("dollos.mcp_server")

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.mcp_server",
        description="MCP server — lets an external AI talk to Doll (peer mode)",
    )
    p.add_argument("--daemon", type=str, default="ws://127.0.0.1:9876",
                    help="Daemon WS URL (default ws://127.0.0.1:9876)")
    p.add_argument("--config", type=Path, required=True,
                    help="Path to mcp.toml ([server] bind_host/bind_port)")
    p.add_argument("--data-root", type=Path, default=Path("data"),
                    help="data root (reserved; default ./data)")
    p.add_argument("--verbose", action="store_true")
    return p


def _load_mcp_config(path: Path) -> tuple[str, int]:
    """Load [server] from mcp.toml. Returns (bind_host, bind_port).

    Fail-closed (spec §E): bind_host MUST be a loopback literal. A non-loopback
    value (e.g. a copied-from-a-tutorial "0.0.0.0") RAISES — the whole threat
    model rests on loopback-only, and there is no network-layer auth to fall
    back to. No fallback: refuse to start rather than open a fail-open hole.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    srv = raw.get("server", {})
    bind_host = srv.get("bind_host", "127.0.0.1")
    bind_port = int(srv.get("bind_port", 9877))
    if bind_host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"mcp.toml [server].bind_host={bind_host!r} is not loopback — "
            "refusing to start. The MCP server is loopback-only (spec §E); "
            "use an SSH tunnel / authenticated reverse proxy for remote access. "
            f"Allowed: {sorted(_LOOPBACK_HOSTS)}."
        )
    return bind_host, bind_port


# conn_uuid grouping: one id per MCP client connection (spec §B.1). Keyed on the
# ServerSession object identity when available. NOTE: correctness does NOT depend
# on this being truly per-connection — call_uuid (uuid4) already makes every
# channel_id globally unique; conn_uuid is only a readability/grouping prefix. If
# ctx.session is unavailable, a fresh uuid per call is equally correct.
_conn_ids: dict[int, str] = {}


def _conn_uuid(ctx: Context) -> str:
    try:
        key = id(ctx.session)
    except Exception:
        return uuid.uuid4().hex[:8]
    cid = _conn_ids.get(key)
    if cid is None:
        cid = uuid.uuid4().hex[:8]
        _conn_ids[key] = cid
    return cid


async def _run_daemon_link(daemon_url: str, link: DaemonLink) -> None:
    """Reconnecting daemon WS client (mirrors bridge __main__ Task 7 loop)."""
    reconnect_delay_s = 5.0
    while True:
        try:
            logger.info("connecting to daemon: %s", daemon_url)
            async with websockets.connect(daemon_url) as ws:
                link.set_ws(ws)
                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    link.dispatch(raw)
        except Exception:
            logger.exception(
                "daemon link dropped — reconnecting in %.0fs", reconnect_delay_s)
        else:
            logger.warning(
                "daemon connection closed cleanly — reconnecting in %.0fs",
                reconnect_delay_s)
        link.set_ws(None)
        await asyncio.sleep(reconnect_delay_s)


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    bind_host, bind_port = _load_mcp_config(args.config)
    link = DaemonLink()

    mcp = FastMCP("DollOS", host=bind_host, port=bind_port)

    @mcp.tool()
    async def talk(name: str, message: str, ctx: Context) -> dict:
        """Send a direct message to Doll and get her reply.

        `name` is how you introduce yourself (self-declared, unverified).
        `message` is what you want to say. Doll decides — with her own
        agency — whether and how much to reply; she may read and stay
        silent. Returns {status, text} where status is one of "reply"
        (she answered), "no_response" (she read it and chose not to
        reply), or "timeout" (no reply within the time limit).
        """
        return await link.talk(_conn_uuid(ctx), name, message)

    await asyncio.gather(
        mcp.run_streamable_http_async(),
        _run_daemon_link(args.daemon, link),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except ValueError as e:
        # fail-closed config error (non-loopback bind_host, bad mcp.toml)
        logger.error("fatal: %s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
