"""`python -m dollbridge --config bridge/discord.toml`, started by dollcore (`[bridge] command`).

Exits non-zero on anything it cannot carry on from (bad config, login refused, missing intent,
the owner not found, dollcore's stream ending); dollcore reports that exit and does not restart it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tomllib
from pathlib import Path

import aiohttp
import discord

from .api import CoreAPI
from .core import Bridge, pump

log = logging.getLogger("dollbridge")


def load_config(path: Path) -> tuple[str, int]:
    """(bot token, owner's Discord user id). Unknown keys are errors, like dollcore's config."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw) - {"token", "owner_id"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    token, owner = raw.get("token"), raw.get("owner_id")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"{path}: token must be the bot token (a non-empty string)")
    if isinstance(owner, str) and owner.isdigit():
        owner = int(owner)
    if not isinstance(owner, int) or owner <= 0:
        raise ValueError(f"{path}: owner_id must be the owner's Discord user id (a number)")
    return token.strip(), owner


def intents() -> discord.Intents:
    i = discord.Intents.none()
    i.guilds = True
    i.dm_messages = True
    i.guild_messages = True     # only so other messages are seen, dropped and logged
    i.message_content = True    # privileged: switch it on in the Developer Portal (DEMO.md)
    return i


async def run(token: str, owner_id: int, base: str, key: str, config: Path) -> int:
    client = discord.Client(intents=intents())
    failure: list[str] = []
    async with aiohttp.ClientSession() as session:
        core = CoreAPI(base, key, session)
        bridge = Bridge(owner_id, core, self_id=lambda: client.user.id if client.user else None)
        stream: asyncio.Task[None] | None = None

        async def stream_then_close() -> None:
            try:
                await pump(core.listen, bridge.on_core)
            except Exception as e:  # noqa: BLE001 — any end of the stream ends the bridge
                failure.append(f"dollcore stream: {e}")
            await client.close()

        @client.event
        async def on_ready() -> None:
            nonlocal stream
            if stream is not None:
                return  # a gateway reconnect: everything is already set up
            try:
                owner = await client.fetch_user(owner_id)
                bridge.dm = owner.dm_channel or await owner.create_dm()
            except Exception as e:  # noqa: BLE001
                failure.append(f"cannot open a DM with owner_id {owner_id}: {e}")
                await client.close()
                return
            log.info("logged in as %s; owner %s; listening to dollcore", client.user, owner_id)
            stream = asyncio.create_task(stream_then_close())

        @client.event
        async def on_message(message: discord.Message) -> None:
            await bridge.on_message(message)

        try:
            await client.start(token)
        except discord.LoginFailure:
            failure.append(f"Discord refused the token in {config}")
        except discord.PrivilegedIntentsRequired:
            failure.append("Message Content Intent is off: switch it on in the Developer Portal → Bot")
        finally:
            if stream is not None:
                stream.cancel()
            if not client.is_closed():
                await client.close()
    if failure:
        log.error("%s", "; ".join(failure))
        return 1
    log.error("Discord connection closed")
    return 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="dollbridge")
    p.add_argument("--config", required=True, type=Path)
    args = p.parse_args()
    base, key = os.environ.get("DOLL_API"), os.environ.get("DOLL_BRIDGE_TOKEN")
    if not base or not key:
        log.error("DOLL_API and DOLL_BRIDGE_TOKEN must be set (dollcore starts this process)")
        sys.exit(2)
    try:
        token, owner_id = load_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        log.error("config: %s", e)
        sys.exit(2)
    sys.exit(asyncio.run(run(token, owner_id, base, key, args.config)))


if __name__ == "__main__":
    main()
