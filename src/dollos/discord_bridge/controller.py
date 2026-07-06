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
import time
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

# owner_guild_only gate (Part B / B2, spec §4.2): per-guild TTL for the
# `is_owner_in_guild` result cache. Short on purpose — an owner leaving a
# guild should stop being trusted within minutes, not hours; this is the
# upper bound on that leak window. Kept short rather than long specifically
# because the safety property this gate provides degrades with cache age.
OWNER_GUILD_CACHE_TTL_S = 300.0


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

    `owner_guild_only` (2026-07-06 spec §4.2, Part B / B2, D7): when `True`
    (the default), only the owner's guilds (+ owner DMs) are forwarded to
    the daemon — everything else is dropped fail-closed by the gate in
    `_capture_and_forward`. `_load_bridge_config` refuses to start
    (`ValueError`) if this is `True` and `owner_id` is empty — a gate that
    trusts only "the owner" but doesn't know who that is has no safe
    semantics to fall back on.

    `channel_allowlist` was removed here (2026-07-06 spec §4.3, Part B /
    B3): it never gated forwarding — P1c already made the bridge
    forward-all, and B2's `owner_guild_only` above is the real gate. Its
    only two remaining uses were seeding `_registered` (reply-routing
    pre-register) and providing the channel list for reconnect-backfill,
    both superseded: register-on-first-forward (`_capture_and_forward`
    below) already covers reply routing on its own, and backfill scope is
    now the separate, optional `backfill_channels` below.

    `backfill_channels` (2026-07-06 spec §4.3, D5(ii)): OPTIONAL list of
    channel ids whose recent history is replayed on reconnect
    (`__main__.py`'s `reconnect_backfill` call) — deliberately DECOUPLED
    from `owner_guild_only`'s wake scope. Backfill runs on EVERY reconnect
    (the ~5s reconnect-loop retries while the bridge is down), so scoping
    it to every owner-guild channel would multiply into N channels x
    `fetch_history` REST calls on every single retry — a real rate-limit
    risk (spec I2). Empty by default: no backfill at all, which is fine —
    the reconnect gap is small and live messages resume immediately either
    way; register-on-first-forward still handles reply routing without it.
    """

    owner_id: str
    bot_id: str | None = None
    owner_guild_only: bool = True
    backfill_channels: list[str] = field(default_factory=list)


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
        # Channels the daemon already holds an external sink for this
        # session. Starts EMPTY (2026-07-06 spec §4.3, Part B / B3:
        # `channel_allowlist`, which used to seed this, is gone) — grown
        # entirely by register-on-first-forward for every channel, DM or
        # guild, static or dynamic (see `_capture_and_forward`). Per-session
        # state — a fresh controller is built on every reconnect, so this
        # can't go stale.
        self._registered: set[str] = set()
        # owner_guild_only gate (Part B / B2): per-guild `is_owner_in_guild`
        # result cache, guild_id -> (result, expiry epoch-seconds). Per-
        # session like `_registered` above — a fresh controller on every
        # reconnect starts with an empty cache, so a stale entry can never
        # outlive a reconnect either.
        self._owner_guild_cache: dict[str, tuple[bool, float]] = {}

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

        Part B / B2 (spec §4.2) adds a SECOND gate after the self-filter:
        `owner_guild_only`. When on, only the owner's guilds (+ owner DMs)
        reach the daemon; a stranger DM, a guild the owner isn't in, or an
        event with no resolvable guild at all are all dropped — fail-closed,
        same as the self-filter, `return` after the (already unconditional)
        ambient append above.
        """
        logged_event = {**event, "date": _event_date(event)}
        if not self._ambient.append(guild_id, channel_id, logged_event):
            return

        if event["author_id"] == self._cfg.bot_id:
            return  # self-filter: logged above, never forwarded.

        author_is_owner = event["author_id"] == self._cfg.owner_id

        if self._cfg.owner_guild_only:
            if event["is_dm"]:
                if not author_is_owner:
                    return  # stranger DM dropped under owner_guild_only
                # owner DM → always forwarded, fall through.
            else:
                guild = event.get("guild")
                if guild is None:
                    return  # no resolvable guild → fail-closed drop
                if not await self._owner_in_guild_cached(guild):
                    return  # not one of the owner's guilds → drop

        # Dynamic register-on-first-FORWARD (was first-wake under P1b; P1c
        # Option A has no local wake concept left to hang it on — see
        # module docstring). The daemon only holds an external sink for a
        # channel it has a ChannelRegister for. Since `channel_allowlist`'s
        # removal (Part B / B3) there is no static pre-register seed at all
        # any more — EVERY channel, a DM (channel id unknowable ahead of
        # time) included, forwards to the daemon with NO external sink for
        # that origin until this fires — `locus_of` defaults "internal" and
        # her reply emits to a local/dummy sink, never back to Discord (this
        # half-breaks the owner-DM path, §3.2/§3.4). Register BEFORE the
        # ChannelEvent so the sink exists by the time a reply might be
        # produced; idempotent via `self._registered` (one register per
        # channel per session).
        if channel_id not in self._registered:
            await self._daemon_send(
                ChannelRegister(
                    channel_id=channel_id, locus="external", kind="discord"
                )
            )
            self._registered.add(channel_id)

        await self._daemon_send(
            ChannelEvent(
                channel_id=channel_id,
                payload={**event, "author_is_owner": author_is_owner},
            )
        )

    async def _owner_in_guild_cached(self, guild_id: str) -> bool:
        """Per-guild short-TTL cache in front of
        `DiscordClient.is_owner_in_guild` (Part B / B2, spec §4.2).

        A cached, non-expired result is returned as-is. Otherwise the real
        check is made — wrapped in try/except so ANY exception (network,
        rate-limit, the client not yet connected, ...) resolves to `False`
        fail-closed. This is defense-in-depth on top of B1's
        `is_owner_in_guild`, which already never raises by contract; a
        gate this security-sensitive must not depend solely on every
        implementation upholding that contract — one uncaught exception
        here must never propagate into `_capture_and_forward` and crash the
        whole forward path.

        The result (True OR False) is cached for `OWNER_GUILD_CACHE_TTL_S`
        either way — a failure is not special-cased to retry sooner; the
        short TTL alone bounds how long a transient failure can suppress a
        legitimate owner message, without introducing a second, separate
        fallback/retry mechanism (CLAUDE.md: no fallback logic).
        """
        now = time.time()
        cached = self._owner_guild_cache.get(guild_id)
        if cached is not None:
            result, expiry = cached
            if now < expiry:
                return result

        try:
            result = await self._discord.is_owner_in_guild(
                guild_id, self._cfg.owner_id
            )
        except Exception:
            logger.exception(
                "is_owner_in_guild raised for guild_id=%s — fail-closed drop",
                guild_id,
            )
            result = False

        # NOTE (Part B whole-branch review M2, 2026-07-06): this caches
        # `result` for the full TTL unconditionally, including a transient-
        # failure `False` (network/rate-limit/`get_guild`-None-during-
        # reconnect) — NOT just a confirmed `discord.NotFound`. That
        # diverges from spec §4.2's failure-mode table, which wants
        # transient failures to fall back to a cached *stale-good* value (or
        # go uncached so the next message retries) rather than pin a `False`
        # for the whole TTL. Deliberate safe-direction availability trade:
        # caching the transient `False` bounds REST-call amplification
        # against a busy guild the owner isn't in; not caching it would
        # reopen that, and a separate retry/stale-good layer on top would be
        # a fallback mechanism (CLAUDE.md: no fallback logic). See the spec
        # note right after the §4.2 table for the full writeup and the
        # tri-state-cache follow-up (`is_owner_in_guild` returning member /
        # not-member / unknown instead of a bare bool) that would let this
        # distinguish the two cases properly.
        self._owner_guild_cache[guild_id] = (result, now + OWNER_GUILD_CACHE_TTL_S)
        return result

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
