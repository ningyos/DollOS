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
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dollos.discord_bridge.wake import l0_wake
from dollos.ipc.messages import AddressedText, ChannelEvent

if TYPE_CHECKING:
    from dollos.discord_bridge.ambient_log import AmbientLog
    from dollos.discord_bridge.client import DiscordClient

logger = logging.getLogger(__name__)

# The daemon connection is fire-and-forget from the bridge's point of view
# too (CLAUDE.md: external actions are fire-and-forget) — daemon_send just
# needs to get the message onto the wire; the real implementation is an
# `await ws.send(msg.model_dump_json())` closure built in `__main__.py`.
DaemonSend = Callable[[ChannelEvent], Awaitable[None]]


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
    ) -> None:
        self._discord = discord
        self._daemon_send = daemon_send
        self._ambient = ambient
        self._cfg = cfg

    async def on_discord_message(self, event: dict) -> None:
        """Full-capture `event`, then wake the daemon iff L0 says so."""
        guild_id = event.get("guild") or "dm"
        logged_event = {**event, "date": _event_date(event)}
        self._ambient.append(guild_id, event["channel_id"], logged_event)

        if not l0_wake(
            event,
            bot_id=self._cfg.bot_id,
            owner_id=self._cfg.owner_id,
            name_aliases=self._cfg.name_aliases,
            always_wake_channels=self._cfg.always_wake_channels,
        ):
            return

        author_is_owner = event["author_id"] == self._cfg.owner_id
        await self._daemon_send(
            ChannelEvent(
                channel_id=event["channel_id"],
                payload={**event, "author_is_owner": author_is_owner},
            )
        )

    async def on_daemon_message(self, msg: object) -> None:
        """Route an `AddressedText` reply back to Discord; ignore the rest."""
        if isinstance(msg, AddressedText):
            await self._discord.send(msg.channel_id, msg.text)


def _event_date(event: dict) -> str:
    """ISO date for AmbientLog's per-day file split.

    Uses the event's own `date` if the caller already stamped one (so a
    backfill replay logs under the original day, not "today"); else today
    (UTC, injected via the wall clock here since live Discord events carry
    no test-controlled clock to thread through).
    """
    return event.get("date") or datetime.now(UTC).date().isoformat()
