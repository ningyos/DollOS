"""`python -m dollbridge --config bridge/discord.toml`, started by dollcore (`[bridge] command`).

Exits non-zero on anything it cannot carry on from (bad config, libopus not loading, login
refused, missing intent, the owner not found, dollcore's stream or `/api/voice` ending, DAVE not
ready after joining voice, the UDP keep-alive failing); dollcore reports that exit and does not
restart it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import discord

from .api import CoreAPI
from .core import Bridge, pump
from .voice import VoiceConfig, VoiceLink, load_opus

log = logging.getLogger("dollbridge")


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int
    voice: VoiceConfig


def load_config(path: Path) -> Config:
    """discord.toml. Unknown keys are errors, like dollcore's config; `[voice]` keys are optional."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw) - {"token", "owner_id", "voice"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    token, owner = raw.get("token"), raw.get("owner_id")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"{path}: token must be the bot token (a non-empty string)")
    if isinstance(owner, str) and owner.isdigit():
        owner = int(owner)
    if not isinstance(owner, int) or owner <= 0:
        raise ValueError(f"{path}: owner_id must be the owner's Discord user id (a number)")
    return Config(token.strip(), owner, VoiceConfig.from_toml(raw.get("voice", {}), str(path)))


def intents() -> discord.Intents:
    i = discord.Intents.none()
    i.guilds = True
    i.dm_messages = True
    i.guild_messages = True     # only so other messages are seen, dropped and logged
    i.message_content = True    # privileged: switch it on in the Developer Portal (DEMO.md)
    i.voice_states = True       # where the owner is in voice, and the bot's own voice connection
    return i


async def run(cfg: Config, base: str, key: str, config: Path) -> int:
    token, owner_id = cfg.token, cfg.owner_id
    client = discord.Client(intents=intents())
    failure: list[str] = []
    async with aiohttp.ClientSession() as session:
        core = CoreAPI(base, key, session)
        bridge = Bridge(owner_id, core, self_id=lambda: client.user.id if client.user else None)
        voice = VoiceLink(cfg.voice, owner_id, client, core.voice)
        stream: asyncio.Task[None] | None = None
        voice_task: asyncio.Task[None] | None = None

        async def stream_then_close() -> None:
            try:
                await pump(core.listen, bridge.on_core)
            except Exception as e:  # noqa: BLE001 — any end of the stream ends the bridge
                failure.append(f"dollcore stream: {e}")
            await client.close()

        async def voice_then_close() -> None:
            try:
                await voice.run()
            except Exception as e:  # noqa: BLE001 — any end of the voice link ends the bridge
                failure.append(f"voice: {e}")
            await client.close()

        @client.event
        async def on_ready() -> None:
            nonlocal stream, voice_task
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
            voice_task = asyncio.create_task(voice_then_close())

        @client.event
        async def on_message(message: discord.Message) -> None:
            await bridge.on_message(message)

        @client.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState,
                                        after: discord.VoiceState) -> None:
            voice.on_voice_state(member, before, after)

        try:
            await client.start(token)
        except discord.LoginFailure:
            failure.append(f"Discord refused the token in {config}")
        except discord.PrivilegedIntentsRequired:
            failure.append("Message Content Intent is off: switch it on in the Developer Portal → Bot")
        finally:
            for task in (stream, voice_task):
                if task is not None:
                    task.cancel()
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
        cfg = load_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        log.error("config: %s", e)
        sys.exit(2)
    try:
        load_opus(cfg.voice.opus_library)
    except ValueError as e:
        log.error("voice: %s (set [voice] opus_library in %s)", e, args.config)
        sys.exit(2)
    sys.exit(asyncio.run(run(cfg, base, key, args.config)))


if __name__ == "__main__":
    main()
