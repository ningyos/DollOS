"""DaemonLink: the testable core of the mcp connector.

Owns the daemon WS send path + demultiplexes inbound AddressedText /
TurnEndAddressed frames back to the per-call collector that is awaiting
them. Every talk() call mints a globally-unique channel_id
``mcp:<conn_uuid>:<call_uuid>`` (spec §B.1 R-DECISION-2) so parallel /
batched calls never collapse into one turn and never cross-deliver.

P2 Task 2 adds a second demux: ``query()`` sends a QueryState/QueryRecent
and awaits its QueryResult, correlated by ``query_id`` instead of
``channel_id`` (spec §C.3). Same collector-map discipline as talk(), just
keyed differently. ``talk()`` also grew an optional ``debug_reliable`` flag
that stamps the outgoing ChannelEvent payload for debug connections.

§B.6: only ``addressed_text`` / ``turn_end_addressed`` / ``query_result``
frames are consumed. Any other server message (a bare ``text_chunk`` /
global ``turn_end`` — an origin-less internal output that does not address
any mcp channel) is ignored, so a talk()/query() result is never polluted
by internal traffic that the SinkResolver's most-recent-internal fallback
may route to this connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from dollos.ipc.messages import ChannelEvent, ChannelRegister, QueryRecent, QueryState

logger = logging.getLogger("dollos.mcp_server")

DEFAULT_TIMEOUT_S = 60.0   # spec §D.3 / §H.6 — hardcoded, NOT an mcp.toml knob
# query() is a local IPC round-trip (daemon reads in-memory state
# synchronously, spec §C.3) — much faster than waiting on an LLM turn, so
# this is a generous safety net, not tuned to talk()'s LLM-latency budget.
# Also hardcoded, NOT an mcp.toml knob (mirrors DEFAULT_TIMEOUT_S's YAGNI).
DEFAULT_QUERY_TIMEOUT_S = 10.0


@dataclass
class _Collector:
    sentences: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _QueryCollector:
    ok: bool = False
    payload: dict = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class DaemonLink:
    def __init__(self) -> None:
        self._ws = None
        self._collectors: dict[str, _Collector] = {}
        self._query_collectors: dict[str, _QueryCollector] = {}

    def set_ws(self, ws) -> None:
        """Bind the current daemon WS (an object with async ``send(str)``).
        Called by __main__'s reconnect loop on each (re)connect."""
        self._ws = ws

    async def _send(self, msg) -> None:
        ws = self._ws
        if ws is None:
            # Explicit fail-closed boundary (no fallback): a talk with no live
            # daemon link surfaces as a clear error, not a silent hang.
            raise RuntimeError("daemon link not connected")
        await ws.send(msg.model_dump_json())

    def dispatch(self, raw: str) -> None:
        """Feed one inbound daemon frame (a JSON string). Routes an
        addressed sentence / turn-end to its channel's collector; ignores
        everything else (§B.6)."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("mcp connector: dropping malformed daemon frame")
            return
        t = data.get("type")
        if t == "addressed_text":
            c = self._collectors.get(data.get("channel_id"))
            if c is not None:
                c.sentences.append(data.get("text", ""))
        elif t == "turn_end_addressed":
            c = self._collectors.get(data.get("channel_id"))
            if c is not None:
                c.done.set()
        elif t == "query_result":
            qc = self._query_collectors.get(data.get("query_id"))
            if qc is not None:
                qc.ok = bool(data.get("ok"))
                qc.payload = data.get("payload") or {}
                qc.done.set()
        # else: bare text_chunk / global turn_end / errors / webrtc → ignore

    async def talk(
        self, conn_uuid: str, name: str, message: str, *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        debug_reliable: bool = False,
    ) -> dict:
        """Send one peer DM and collect Doll's reply for this call's channel.

        Returns a structured, discriminated outcome (spec §D.3):
          {"status": "reply",       "text": <joined sentences>}   # she replied
          {"status": "no_response", "text": ""}                   # read, chose silence
          {"status": "timeout",     "text": <partial, maybe "">}  # no turn-end in time

        ``debug_reliable=True`` (P2 Task 2, spec §C.2) stamps
        ``debug_reliable: True`` on the outgoing ChannelEvent payload — a
        best-effort prompt-level nudge toward a substantive reply, NOT a
        tier/capability change. Omitted entirely (not `False`) for ordinary
        peer calls, so a non-debug ChannelEvent payload is byte-identical
        to pre-P2.
        """
        call_uuid = uuid.uuid4().hex
        channel_id = f"mcp:{conn_uuid}:{call_uuid}"
        collector = _Collector()
        self._collectors[channel_id] = collector
        try:
            # register-on-first (this call's channel is unique, so always new),
            # then the ChannelEvent — mirror the discord DM payload (§B.2).
            await self._send(
                ChannelRegister(channel_id=channel_id, locus="external", kind="mcp")
            )
            payload = {
                "channel_id": channel_id,     # envelope overwrites; kept consistent
                "author_id": f"mcp:{name}",   # self-declared, unverified (continuity)
                "author": name,               # render display name
                "is_dm": True,                # drives l0_dm admit + engaged window
                "author_is_owner": False,     # a peer is NEVER the owner (§E)
                "content": message,            # render reads content
                "channel_kind": "mcp",        # situational discriminator (§B.4)
                "ts": time.time(),
            }
            if debug_reliable:
                payload["debug_reliable"] = True
            await self._send(ChannelEvent(channel_id=channel_id, payload=payload))
            try:
                await asyncio.wait_for(collector.done.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return {"status": "timeout", "text": "".join(collector.sentences)}
            if not collector.sentences:
                return {"status": "no_response", "text": ""}
            return {"status": "reply", "text": "".join(collector.sentences)}
        finally:
            # one-shot: unload this channel's collector so the map can't grow
            self._collectors.pop(channel_id, None)

    async def query(
        self, msg: QueryState | QueryRecent, *,
        timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
    ) -> dict:
        """Send a QueryState/QueryRecent and await its QueryResult, correlated
        by ``query_id`` (spec §C.3). Fail-closed: a timeout or an
        ``ok=False`` QueryResult RAISES rather than returning fabricated or
        partial data — a debug introspection tool must never silently lie
        about Doll's state.

        Callers (the connector's get_state/get_recent tools) are expected
        to mint a fresh ``query_id`` per call and pass an already-built
        QueryState/QueryRecent (with the mcp.toml query_token stamped in).
        """
        collector = _QueryCollector()
        self._query_collectors[msg.query_id] = collector
        try:
            await self._send(msg)
            try:
                await asyncio.wait_for(collector.done.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"query {msg.query_id} ({msg.type}) timed out waiting for daemon"
                ) from None
            if not collector.ok:
                raise RuntimeError(
                    f"query {msg.query_id} ({msg.type}) rejected by daemon "
                    "(missing/invalid query_token, or the query surface is disabled)"
                )
            return collector.payload
        finally:
            # one-shot: unload this query's collector so the map can't grow
            self._query_collectors.pop(msg.query_id, None)
