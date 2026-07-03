"""BridgeController — wires DiscordClient <-> daemon <-> AmbientLog (P1b Task 5).

Two handlers, matching the plan's brief exactly:

- `on_discord_message(event)`: ALWAYS full-captures the event to `AmbientLog`
  first (spec §3.3 — finetune corpus / audit trail, unconditional, including
  the bot's own messages and unrelated stranger chatter). THEN decides
  whether to wake the daemon via `l0_wake` (spec §3.4 L0 + self-filter C3);
  if it wakes, sends a `ChannelEvent` with `author_is_owner` derived from
  `cfg.owner_id` (numeric id match — spec §3.4 identity binding).
- `on_daemon_message(msg)`: routes an `AddressedText` reply back to Discord
  via `discord.send(channel_id, text)`. Any other server message type is not
  meaningful to this bridge and is ignored.

Full capture and the wake decision are deliberately two separate steps, not
one — ambient logging must never depend on L0 passing (spec §3.3 C3: a
stranger's unrelated chatter, and the bot's own messages, still get logged;
they just never become a `ChannelMessage` perception).

Task 6 adds two more things on top of Task 5's two handlers:

- `backfill(guild, channel, recent_events)`: replays a reconnect-gap batch of
  events (e.g. from Discord channel history) through the SAME capture+wake
  path as live messages, `_capture_and_maybe_wake` — shared with
  `on_discord_message` so a duplicate msg_id (AmbientLog.append's False
  return) is skipped ENTIRELY: no re-log, no re-evaluated L0, no re-fired
  ChannelEvent (carry I-2 idempotency; R2 finding: without this, backfill
  after a crash-restart could double-reply to an already-answered mention).
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
from dollos.discord_bridge.wake import l0_wake
from dollos.ipc.messages import AddressedText, ChannelEvent, ChannelRegister

if TYPE_CHECKING:
    from dollos.discord_bridge.ambient_log import AmbientLog
    from dollos.discord_bridge.client import DiscordClient

logger = logging.getLogger(__name__)

# The daemon connection is fire-and-forget from the bridge's point of view
# too (CLAUDE.md: external actions are fire-and-forget) — daemon_send just
# needs to get the message onto the wire; the real implementation is an
# `await ws.send(msg.model_dump_json())` closure built in `__main__.py`.
# Carries both ChannelRegister (dynamic register-on-first-wake, see
# `_capture_and_maybe_wake`) and ChannelEvent.
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
    """

    owner_id: str
    name_aliases: list[str]
    always_wake_channels: set[str] = field(default_factory=set)
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
        # on connect), then grown by register-on-first-wake for DMs and any
        # non-allowlisted channel a wake fires from (see
        # `_capture_and_maybe_wake`). Per-session state — a fresh controller
        # is built on every reconnect, so this can't go stale.
        self._registered: set[str] = set(cfg.channel_allowlist)

    async def on_discord_message(self, event: dict) -> None:
        """Full-capture `event`, then wake the daemon iff L0 says so."""
        guild_id = event.get("guild") or "dm"
        await self._capture_and_maybe_wake(guild_id, event["channel_id"], event)

    async def backfill(
        self, guild: str, channel: str, recent_events: list[dict]
    ) -> None:
        """Replay a reconnect-gap batch of `recent_events` (e.g. fetched from
        Discord channel history) through the same capture+wake path as live
        messages.

        `guild`/`channel` are the authoritative scope for this call (the
        reconnect context the events were fetched under), not re-derived per
        event. Each event goes through `_capture_and_maybe_wake`, so a
        msg_id already in the ambient log (AmbientLog.append's False return)
        is skipped ENTIRELY — no re-log, no re-evaluated L0, no re-fired
        ChannelEvent (carry I-2 idempotency).
        """
        for event in recent_events:
            await self._capture_and_maybe_wake(guild, channel, event)

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
        through the SAME capture+wake path as live traffic and `backfill()`,
        `_capture_and_maybe_wake` — a msg_id already in the ambient log is
        skipped ENTIRELY, no re-log, no re-fired ChannelEvent (carry I-2
        idempotency; R2 finding: without this, reconnecting after a crash or
        a bridge restart — the "reconnect after a kill" live-smoke case —
        could double-reply to an already-answered mention).

        `guild_id` is derived per event the same way `on_discord_message`
        does (`event.get("guild") or "dm"`) since `fetch` is scoped to a
        channel, not a guild, and channel history can in principle span
        guild membership changes.
        """
        for channel_id in channels:
            events = await fetch(channel_id, limit)
            for event in events:
                guild_id = event.get("guild") or "dm"
                await self._capture_and_maybe_wake(guild_id, channel_id, event)

    async def _capture_and_maybe_wake(
        self, guild_id: str, channel_id: str, event: dict
    ) -> None:
        """Shared core of `on_discord_message` and `backfill`: full-capture
        first via `AmbientLog.append`; a duplicate msg_id (False return)
        short-circuits here and is never re-evaluated by L0 or re-sent as a
        ChannelEvent (carry I-2 idempotency)."""
        logged_event = {**event, "date": _event_date(event)}
        if not self._ambient.append(guild_id, channel_id, logged_event):
            return

        if not l0_wake(
            event,
            bot_id=self._cfg.bot_id,
            owner_id=self._cfg.owner_id,
            name_aliases=self._cfg.name_aliases,
            always_wake_channels=self._cfg.always_wake_channels,
        ):
            return

        # Dynamic register-on-first-wake (P1b review): the daemon only holds
        # an external sink for a channel it has a ChannelRegister for.
        # `__main__.py` pre-registers the static allowlist on connect, but a
        # DM (channel id unknowable ahead of time) or a mention in a
        # non-allowlisted channel would otherwise wake the daemon with NO
        # external sink for that origin — `locus_of` defaults "internal" and
        # her reply emits to a local/dummy sink, never back to Discord (this
        # half-breaks the owner-DM path, §3.2/§3.4). Register BEFORE the
        # ChannelEvent so the sink exists by the time the reply is produced;
        # idempotent via `self._registered` (one register per channel per
        # session, allowlisted channels already seeded so never re-sent).
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
