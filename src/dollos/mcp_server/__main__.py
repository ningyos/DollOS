"""dollos-mcp CLI.

Usage:
    python -m dollos.mcp_server --daemon ws://127.0.0.1:9876 \\
        --config mcp.toml --data-root data

Runs TWO roles in one process:
  1. outward: a FastMCP streamable-HTTP server bound loopback (mcp.toml
     [server] bind_host/bind_port). bind_host is fail-closed validated to a
     loopback literal (spec §E) — any other value raises and refuses to
     start. Exposes the peer tool talk(name, message) plus, for debug
     connections (P2 Task 2), authenticate(secret) / get_state() /
     get_recent(n).
  2. inward: a daemon IPC WS client (reconnecting), feeding inbound frames
     to DaemonLink.dispatch and sending ChannelRegister/ChannelEvent/
     QueryState/QueryRecent out.

Thin untested wiring on purpose — all IPC-mapping logic lives in
DaemonLink (tests/test_mcp_daemon_link.py, tests/test_mcp_debug_mode.py).
Mirrors discord_bridge/__main__.py.

P2 Task 2 — debug mode secret gate (spec §C.1, grounded correction I1):
FastMCP (mcp 1.28.1) exposes a GLOBAL tool set — it cannot show different
tools per connection. So get_state/get_recent are ALWAYS registered; their
BODIES hard-check a per-session ``_authed`` flag before touching
DaemonLink.query(...). That per-session check (not the daemon's
query_token, which every connector call carries regardless of debug
status) IS the access control for MCP clients — see _require_debug.
The gate primitives (_try_authenticate / _require_debug) are pulled out as
plain functions precisely so they're unit-testable without a real MCP
Context/session (tests/test_mcp_debug_mode.py).
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

from dollos.ipc.messages import QueryRecent, QueryState
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


def _load_debug_config(path: Path) -> tuple[str, str]:
    """Load [server].debug_secret / query_token from mcp.toml (spec §C.1/§C.3).

    Both default to "" (empty) when unset. Empty debug_secret → debug mode
    is disabled entirely: _try_authenticate NEVER succeeds (see below), so
    get_state/get_recent stay permanently gated and talk() never stamps
    debug_reliable — fail-closed, no separate "is debug configured" branch
    to get out of sync with the auth check itself.
    """
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    srv = raw.get("server", {})
    debug_secret = srv.get("debug_secret") or ""
    query_token = srv.get("query_token") or ""
    return debug_secret, query_token


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


# P2 Task 2 — debug mode per-session state (spec §C.1). Keyed the same way as
# _conn_ids (id(ctx.session)). A session id in this set has presented the
# correct mcp.toml debug_secret via authenticate(secret) and is allowed to
# call get_state/get_recent and gets debug_reliable=True talk().
_authed: set[int] = set()


def _try_authenticate(session_id: int, secret: str, debug_secret: str) -> bool:
    """Fail-closed secret compare + per-session ``_authed`` mutation.

    Pulled out as a plain function (no Context/FastMCP involved) so the
    gate itself is unit-testable (tests/test_mcp_debug_mode.py) without a
    real MCP client. Empty/unset ``debug_secret`` NEVER authenticates —
    debug mode is disabled entirely, not "any secret works". No exception
    on mismatch, no leak of whether a secret is even configured — the
    caller (the `authenticate` tool) always returns the same shape either
    way.
    """
    if debug_secret and secret == debug_secret:
        _authed.add(session_id)
        return True
    return False


def _require_debug(session_id: int) -> None:
    """Hard per-session gate for get_state/get_recent (spec §C.1, grounded
    correction I1). This is REAL enforcement, not cosmetic: FastMCP exposes
    a single global tool set, so this body-level check — not tool
    registration, not the daemon's query_token (every connector call
    carries that regardless of debug status) — is the only thing stopping
    any local MCP client from reading mood/current_self/recent
    external_public interactions. Raises so the MCP tool call surfaces as
    an explicit error to the caller, never a silent empty/partial result.
    """
    if session_id not in _authed:
        raise PermissionError(
            "not authenticated — call authenticate(secret) with the debug "
            "secret first (get_state/get_recent are debug-only tools)"
        )


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
    debug_secret, query_token = _load_debug_config(args.config)
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

        On an authenticated debug connection (see `authenticate`), this
        nudges Doll toward a substantive reply (best-effort, spec §C.2) —
        it does not unlock any new capability.
        """
        debug_reliable = id(ctx.session) in _authed
        return await link.talk(
            _conn_uuid(ctx), name, message, debug_reliable=debug_reliable
        )

    @mcp.tool()
    async def authenticate(secret: str, ctx: Context) -> dict:
        """Present the debug secret to unlock get_state/get_recent
        introspection and reliable-reply talk() for this connection.

        Returns {"debug": true} on a correct secret, {"debug": false}
        otherwise (wrong secret, no secret presented, or debug mode
        disabled in mcp.toml) — no exception, no indication of whether a
        secret is even configured.
        """
        ok = _try_authenticate(id(ctx.session), secret, debug_secret)
        return {"debug": ok}

    @mcp.tool()
    async def get_state(ctx: Context) -> dict:
        """Debug-only: snapshot Doll's self-state — {"mood": str,
        "current_self": str}. Requires a prior authenticate(secret) call
        with the correct debug secret on this connection; otherwise raises.
        """
        _require_debug(id(ctx.session))
        return await link.query(QueryState(query_id=uuid.uuid4().hex, token=query_token))

    @mcp.tool()
    async def get_recent(ctx: Context, n: int = 20) -> dict:
        """Debug-only: the most recent (up to `n`, daemon-clamped to 100)
        external_public-origin interactions — {"items": [{"kind", "text",
        "ts"}, ...]}. Never includes owner-private conversation. Requires
        a prior authenticate(secret) call with the correct debug secret on
        this connection; otherwise raises.
        """
        _require_debug(id(ctx.session))
        return await link.query(
            QueryRecent(query_id=uuid.uuid4().hex, token=query_token, n=n)
        )

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
