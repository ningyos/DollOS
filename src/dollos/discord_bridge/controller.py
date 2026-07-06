"""BridgeController — wires DiscordClient <-> daemon <-> AmbientLog (P1b Task 5).

P1c Task 3 (Option A, spec §3.4 "全量訊息都送 daemon"): the bridge no longer
decides wake locally. It is a dumb forwarder — L0/L1/L2 admission moved
daemon-side into `AttentionGate` (`mind/attention.py`, Task 1/2/4). Two
handlers:

- `on_discord_message(event)`: ALWAYS full-captures the event to `AmbientLog`
  first (spec §3.3 — finetune corpus / audit trail, unconditional, including
  the bot's own messages and unrelated stranger chatter). THEN forwards it
  to the daemon as a `ChannelEvent` UNLESS it is self-authored (spec §3.3
  C3 self-filter — the one gate left on this side; her own messages must
  never reach the daemon, or the self-echo loop P1b fixed comes back).
  `author_is_owner` is derived from `cfg.owner_id` (numeric id match — spec
  §3.4 identity binding).
- `on_daemon_message(msg)`: routes an `AddressedText` reply back to Discord
  via `discord.send(channel_id, text)`. Any other server message type is not
  meaningful to this bridge and is ignored.

Full capture and the forward decision are deliberately two separate steps,
not one — ambient logging must never depend on the self-filter passing (spec
§3.3 C3: the bot's own messages still get logged, they just never get
forwarded).

Task 6 adds two more things on top of Task 5's two handlers:

- `backfill(guild, channel, recent_events)`: replays a reconnect-gap batch of
  events (e.g. from Discord channel history) through the SAME capture+forward
  path as live messages, `_capture_and_forward` — shared with
  `on_discord_message` so a duplicate msg_id (AmbientLog.append's False
  return) is skipped ENTIRELY: no re-log, no re-forwarded ChannelEvent
  (carry I-2 idempotency; R2 finding: without this, backfill after a
  crash-restart could double-deliver an already-seen message).
- 429-aware send: `on_daemon_message` routes through `_send_with_retry`,
  which catches `client.RateLimited(retry_after)` from `discord.send` and
  retries exactly once after `await self._sleep(retry_after)` — `_sleep` is
  injectable (defaults to `asyncio.sleep`) so tests never actually wait.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dollos.discord_bridge.client import RateLimited
from dollos.ipc.messages import AddressedText, ChannelEvent, ChannelRegister

if TYPE_CHECKING:
    from dollos.discord_bridge.ambient_log import AmbientLog
    from dollos.discord_bridge.client import DiscordClient

logger = logging.getLogger(__name__)

# The daemon connection is fire-and-forget from the bridge's point of view
# too (CLAUDE.md: external actions are fire-and-forget) — daemon_send just
# needs to get the message onto the wire; the real implementation is an
# `await ws.send(msg.model_dump_json())` closure built in `__main__.py`.
# Carries both ChannelRegister (dynamic register-on-first-forward, see
# `_capture_and_forward`) and ChannelEvent.
DaemonSend = Callable[[ChannelEvent | ChannelRegister], Awaitable[None]]

# Injectable clock for the 429 retry sleep — defaults to real asyncio.sleep;
# tests substitute a recording no-op so a retry test never actually waits.
SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class BridgeConfig:
    """Bridge-static config (spec §3.1 `[discord]`).

    `bot_id` is the one field that is NOT known at config-load time: it's
    Doll's own Discord user id, only resolvable via `DiscordClient.me_id()`
    once the client has actually connected (see `client.DiscordClient`'s
    docstring). Real deployment (`__main__.py`) fills it in lazily on the
    first Discord event delivered; tests construct `BridgeConfig` with it
    already set, since `FakeDiscordClient.me_id()` never needs a connection.

    `name_aliases` / `always_wake_channels` were removed here (2026-07-06
    self-learned-aliases spec §3.6, Part A A5): both were dead config — P1c
    already moved L0/L1 wake admission daemon-side into `AttentionGate`, and
    A3 confirmed the daemon builds its `alias_provider` from the pack seed +
    `AttentionSettings.name_aliases`/`always_wake_channels` (the KEPT admin
    floor, `dollos.config.AttentionSettings`) + learned tokens — the bridge
    never read either field for anything.
    """

    owner_id: str
    channel_allowlist: list[str] = field(default_factory=list)
    bot_id: str | None = None


class BridgeController:
    """Drives the two directions of traffic through a Discord bridge connection."""

    def __init__(
        self,
        discord: DiscordClient,
        daemon_send: DaemonSend,
        ambient: AmbientLog,
        cfg: BridgeConfig,
        *,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._discord = discord
        self._daemon_send = daemon_send
        self._ambient = ambient
        self._cfg = cfg
        self._sleep = sleep
        # Channels the daemon already holds an external sink for this session.
        # Seeded with the static allowlist (`__main__.py` pre-registers those
        # on connect), then grown by register-on-first-forward for DMs and
        # any non-allowlisted channel a forward comes from (see
        # `_capture_and_forward`). Per-session state — a fresh controller
        # is built on every reconnect, so this can't go stale.
        self._registered: set[str] = set(cfg.channel_allowlist)

    async def on_discord_message(self, event: dict) -> None:
        """Full-capture `event`, then forward it to the daemon unless it's
        self-authored (P1c Option A: forward-all, L0/L1 moved daemon-side)."""
        guild_id = event.get("guild") or "dm"
        await self._capture_and_forward(guild_id, event["channel_id"], event)

    async def backfill(
        self, guild: str, channel: str, recent_events: list[dict]
    ) -> None:
        """Replay a reconnect-gap batch of `recent_events` (e.g. fetched from
        Discord channel history) through the same capture+forward path as
        live messages.

        `guild`/`channel` are the authoritative scope for this call (the
        reconnect context the events were fetched under), not re-derived per
        event. Each event goes through `_capture_and_forward`, so a msg_id
        already in the ambient log (AmbientLog.append's False return) is
        skipped ENTIRELY — no re-log, no re-forwarded ChannelEvent (carry
        I-2 idempotency).
        """
        for event in recent_events:
            await self._capture_and_forward(guild, channel, event)

    async def reconnect_backfill(
        self,
        fetch: Callable[[str, int], Awaitable[list[dict]]],
        channels: list[str],
        *,
        limit: int = 50,
    ) -> None:
        """Reconnect-gap recovery (Task 7): for each channel id in
        `channels`, fetch its recent history via `fetch(channel_id, limit)`
        (real deployment: `DiscordClient.fetch_history`, see client.py — the
        translator there always stamps a true `ts`) and replay every event
        through the SAME capture+forward path as live traffic and
        `backfill()`, `_capture_and_forward` — a msg_id already in the
        ambient log is skipped ENTIRELY, no re-log, no re-forwarded
        ChannelEvent (carry I-2 idempotency; R2 finding: without this,
        reconnecting after a crash or a bridge restart — the "reconnect
        after a kill" live-smoke case — could double-deliver an
        already-seen message).

        `guild_id` is derived per event the same way `on_discord_message`
        does (`event.get("guild") or "dm"`) since `fetch` is scoped to a
        channel, not a guild, and channel history can in principle span
        guild membership changes.
        """
        for channel_id in channels:
            events = await fetch(channel_id, limit)
            for event in events:
                guild_id = event.get("guild") or "dm"
                await self._capture_and_forward(guild_id, channel_id, event)

    async def _capture_and_forward(
        self, guild_id: str, channel_id: str, event: dict
    ) -> None:
        """Shared core of `on_discord_message`, `backfill`, and
        `reconnect_backfill`: full-capture first via `AmbientLog.append`; a
        duplicate msg_id (False return) short-circuits here and is never
        re-forwarded (carry I-2 idempotency).

        P1c Task 3 (Option A): L0/L1/L2 admission no longer happens here —
        it moved daemon-side into `AttentionGate` (`mind/attention.py`, Task
        1/2/4). This method forwards every event to the daemon UNLESS it is
        self-authored — the self-filter is the ONLY gate left on this side
        (spec §3.3 C3: losing it re-introduces the self-echo infinite loop
        P1b fixed). The old `discord_bridge/wake.py::l0_wake` L0 signal
        logic (DM/mention/name-alias/always-wake) is gone from this path
        entirely; it now lives in `AttentionGate._l0_signal`.
        """
        logged_event = {**event, "date": _event_date(event)}
        if not self._ambient.append(guild_id, channel_id, logged_event):
            return

        if event["author_id"] == self._cfg.bot_id:
            return  # self-filter: logged above, never forwarded.

        # Dynamic register-on-first-FORWARD (was first-wake under P1b; P1c
        # Option A has no local wake concept left to hang it on — see
        # module docstring). The daemon only holds an external sink for a
        # channel it has a ChannelRegister for. `__main__.py` pre-registers
        # the static allowlist on connect, but a DM (channel id unknowable
        # ahead of time) or any non-allowlisted channel would otherwise
        # forward to the daemon with NO external sink for that origin —
        # `locus_of` defaults "internal" and her reply emits to a
        # local/dummy sink, never back to Discord (this half-breaks the
        # owner-DM path, §3.2/§3.4). Register BEFORE the ChannelEvent so the
        # sink exists by the time a reply might be produced; idempotent via
        # `self._registered` (one register per channel per session,
        # allowlisted channels already seeded so never re-sent).
        if channel_id not in self._registered:
            await self._daemon_send(
                ChannelRegister(
                    channel_id=channel_id, locus="external", kind="discord"
                )
            )
            self._registered.add(channel_id)

        author_is_owner = event["author_id"] == self._cfg.owner_id
        await self._daemon_send(
            ChannelEvent(
                channel_id=channel_id,
                payload={**event, "author_is_owner": author_is_owner},
            )
        )

    async def on_daemon_message(self, msg: object) -> None:
        """Route an `AddressedText` reply back to Discord; ignore the rest."""
        if isinstance(msg, AddressedText):
            await self._send_with_retry(msg.channel_id, msg.text)

    async def _send_with_retry(self, channel_id: str, text: str) -> None:
        """`discord.send`, retried ONCE after a 429 (R2 finding: Discord
        rate-limit handling). `RateLimited.retry_after` is the delay Discord
        itself reports; the wait goes through `self._sleep` (injectable
        clock) so a retry test never actually waits. A second failure is not
        caught — it propagates, rather than retrying unboundedly."""
        try:
            await self._discord.send(channel_id, text)
        except RateLimited as exc:
            await self._sleep(exc.retry_after)
            await self._discord.send(channel_id, text)


def _event_date(event: dict) -> str:
    """ISO date for AmbientLog's per-day file split.

    Prefers the event's TRUE Discord timestamp (`ts`, epoch seconds) when
    present — `datetime.fromtimestamp(ts, UTC).date()` — so a backfilled
    event near a UTC midnight boundary buckets by the day it was actually
    POSTED, not the day it happened to be replayed (LANDMINE, review: without
    this the same msg_id can land in two different date files across
    midnight and re-wake the daemon on a later replay). `client.py`'s
    `fetch_history` translator always stamps `ts`, so every live and
    backfilled Discord event takes this path.

    Falls back to the caller's pre-stamped `date` (if any), else today's
    wall clock, ONLY for an event with no `ts` at all — kept as a
    best-effort fallback; no real Discord event should ever lack a `ts`.
    """
    ts = event.get("ts")
    if ts is not None:
        return datetime.fromtimestamp(ts, UTC).date().isoformat()
    return event.get("date") or datetime.now(UTC).date().isoformat()
