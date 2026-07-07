"""
voice_receive_spike.py — P3a de-risking spike: can py-cord deliver per-user
Discord voice as an INCREMENTAL stream (not just record-to-file)?

============================================================================
THE QUESTION THIS ANSWERS
============================================================================
DollOS voice-calls architecture C needs per-user audio arriving incrementally
in real time, so a bridge process can run SileroVAD endpointing on each
user's stream and cut discrete utterances. Discord does not officially
support bots receiving voice; py-cord's receive path is reverse-engineered.
This script proves (or disproves) that a custom `discord.sinks.Sink`
subclass's `write(data, user)` fires PER PACKET, continuously, during the
call — as opposed to only once at `stop_recording()`.

Source-inspection already answered the architecture question (see the
spike report / commit message this file shipped with): yes, py-cord's
`PacketRouter._do_run()` (discord/voice/receive/router.py) calls
`self.sink.write(data, data.source)` inside a background-thread loop driven
by a small per-SSRC jitter buffer (~1 packet / ~20ms preferred size), NOT
at `stop_recording()`. This script is the live, human-in-the-loop
confirmation of that source read against a real Discord voice channel.

============================================================================
KNOWN CONCRETE BUGS IN THE INSTALLED py-cord 2.8.0 (found by source read +
zero-network repro — see the workarounds in `_StreamingSink` below)
============================================================================
1. `discord.sinks.Sink` (and every official Sink subclass: WaveSink,
   PCMSink, MP3Sink, ...) does NOT define `__sink_listeners__` or
   `walk_children()`, both of which `SinkEventRouter.__init__` reads
   unconditionally. Calling `vc.start_recording(WaveSink(), cb)` raises
   `AttributeError: 'WaveSink' object has no attribute '__sink_listeners__'`
   immediately — before a single voice packet is processed. Reproduced
   directly with no Discord connection:
       python -c "
       import discord.sinks as sinks
       from discord.voice.receive.router import SinkEventRouter
       SinkEventRouter(sinks.WaveSink(), reader=None)
       "
   -> AttributeError. `_StreamingSink` below defines both attributes as a
   workaround.
2. `discord.sinks.Sink` also never defines `is_opus()` (it only exists on
   the unrelated *playback* `AudioSource` classes in `discord/player.py`).
   `PacketDecoder.__init__` (discord/opus.py) calls `self.sink.is_opus()`
   the first time it sees a new SSRC -> AttributeError on first packet.
   `_StreamingSink` defines `is_opus() -> False` to get decoded PCM.
3. `discord.sinks.Sink.write()`'s default implementation writes the raw
   `data` argument straight into an `io.BytesIO` — but the `data` argument
   is actually a `discord.voice.VoiceData` object (packet + source + pcm),
   not bytes. Using the base implementation as-is would TypeError. Our
   sink overrides `write()` and pulls `data.pcm` explicitly.
4. DAVE (Discord's mandatory E2EE for voice, enforced globally since
   2026-03-02 per github.com/Pycord-Development/pycord/issues/3135) is
   decrypted in `PacketDecoder._decode_packet` (discord/opus.py) AFTER the
   Opus decode step. The community's own diagnosis (see PR discussion on
   pycord#2873 / #3159) is that this ordering is backwards and causes
   `OpusError: corrupted stream` on real DAVE-encrypted channels — which,
   since enforcement, is EVERY channel. That exception is NOT caught
   per-packet: it propagates out of `PacketRouter._do_run()` and the
   `finally:` in `AudioReader.run()` calls `stop_recording()`, killing
   receive for ALL users, not just the offending packet.
   As of the most recent activity we found (2026-04-27,
   pycord#3139 comments), a Pycord maintainer said there is no ETA for
   receive to be "fully functional"; a community fix
   (github.com/Pycord-Development/pycord/pull/3159, branch
   `fix/voice-rec-2`) exists and was tester-validated but is NOT merged
   and NOT in any PyPI release as of py-cord 2.8.0 (2026-05-18).
   >>> Practical upshot: this script may run cleanly for a while and then
   >>> die with `OpusError: corrupted stream` when DAVE rekeys (e.g. anyone
   >>> joins/leaves the channel) or during the handshake window right after
   >>> connecting. That failure mode IS the finding, not a bug in this spike.

============================================================================
EXACT RUN INSTRUCTIONS (for a human — this script is NOT run by the agent)
============================================================================
1. STOP THE LIVE BRIDGE FIRST. Two gateway connections on the same bot
   token race each other and Discord will reject the second login.
       uv run dollosctl stop
   (or use a second, throwaway bot token/application for this spike so you
   don't have to stop anything — either works.)

2. Install voice extras into the project venv (NOT added to pyproject.toml
   / uv.lock by this spike — this is a throwaway install; drop it after):
       uv pip install pynacl davey
   `davey` needs Python 3.10-3.13 (matches this repo's 3.13). If libopus
   is missing at runtime (`discord.opus.is_loaded()` is False), install
   the system package:
       sudo apt install libopus0        # Debian/Ubuntu
   (On this machine libopus0 was already present via apt; py-cord/ctypes
   should find it automatically via `ctypes.util.find_library("opus")`.)

3. Get a voice channel's guild ID and channel ID (right-click in Discord
   with Developer Mode on -> Copy ID). Use a small/private test server.

4. Run it, then JOIN THAT VOICE CHANNEL YOURSELF AND SPEAK for most of the
   ~20s window (silence produces zero packets — Discord only sends RTP
   while you're actually talking):
       DISCORD_BOT_TOKEN=... uv run python experiments/voice_receive_spike.py \\
           --guild-id 123456789012345678 \\
           --channel-id 123456789012345678 \\
           --duration 20
   or pass --token instead of the env var.

5. Read the printed report. SUCCESS looks like:
     - "first packet" latency of well under 1s after you start speaking
     - a `write() call` log line per ~20ms of speech (dozens to hundreds
       of lines for a 20s window with a few seconds of talking), each
       tagged with your user id and increasing byte counts — i.e. packets
       arriving THROUGHOUT the run, not one write() call at the very end
     - a final per-user summary with packet_count > 1 and
       last_ts - first_ts spanning multiple seconds
   FAILURE / regression looks like:
     - an `OpusError: corrupted stream` traceback killing the recording
       partway through (this is the known DAVE-decrypt-order bug above —
       still counts as evidence, note when in the run it happened)
     - zero packets logged despite speaking (check: bot actually joined
       the channel? not server-deafened? PyNaCl/davey/libopus present?)

============================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "P3a spike: prove/disprove per-user INCREMENTAL voice receive "
            "in py-cord via a custom streaming Sink. Throwaway script, not "
            "wired into the daemon or discord-bridge."
        ),
    )
    p.add_argument(
        "--token",
        default=None,
        help="Discord bot token. Falls back to $DISCORD_BOT_TOKEN env var. "
        "Never hardcode this; never read bridge.toml from this script.",
    )
    p.add_argument(
        "--guild-id",
        type=int,
        required=True,
        help="Guild (server) ID that owns the target voice channel.",
    )
    p.add_argument(
        "--channel-id",
        type=int,
        required=True,
        help="Voice channel ID to join and record from.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="Seconds to record before leaving and printing the report "
        "(default: 20).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)  # argparse handles --help / -h and exits here,
    # before any discord import is attempted, so `--help` works even
    # without pynacl/davey installed.

    token = args.token or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print(
            "error: no token. Pass --token or set DISCORD_BOT_TOKEN.",
            file=sys.stderr,
        )
        return 2

    # Import discord lazily (after arg parsing) so --help never needs the
    # voice extras (pynacl/davey) to be installed. Actually running the
    # spike does need them (discord.voice raises MissingVoiceDependenciesError
    # at import time otherwise) — see the docstring's install step.
    try:
        import discord
        from discord.sinks import Sink
    except Exception as exc:  # pragma: no cover - human-run diagnostic path
        print(f"error: failed to import discord: {exc}", file=sys.stderr)
        return 2

    # --- per-packet stats, keyed by discord user id -----------------------
    stats: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "bytes": 0, "first_ts": None, "last_ts": None}
    )
    run_start = time.monotonic()

    class _StreamingSink(Sink):
        """Custom Sink proving incremental per-packet delivery.

        Overrides `write()` to log+tally on every call instead of buffering
        to a file. Also defines three methods/attributes that the base
        `discord.sinks.Sink` in the installed py-cord 2.8.0 does NOT define
        but that `SinkEventRouter`/`PacketDecoder` unconditionally read —
        see bugs (1)-(3) in this file's module docstring. Without these,
        `start_recording()` AttributeErrors before any audio flows.
        """

        __sink_listeners__: list = []  # workaround for bug (1)

        def walk_children(self, with_self: bool = False):  # workaround for bug (1)
            return [self] if with_self else []

        def is_opus(self) -> bool:  # workaround for bug (2)
            return False  # False => PacketDecoder hands us decoded PCM via data.pcm

        def write(self, data, user) -> None:  # overrides the broken base impl (bug 3)
            now = time.monotonic()
            user_id = getattr(user, "id", user)
            pcm_len = len(data.pcm) if getattr(data, "pcm", None) else 0
            s = stats[user_id]
            if s["first_ts"] is None:
                s["first_ts"] = now
            s["last_ts"] = now
            s["count"] += 1
            s["bytes"] += pcm_len
            elapsed = now - run_start
            print(
                f"[write()] t={elapsed:7.3f}s user={user_id} "
                f"bytes={pcm_len} total_bytes={s['bytes']} "
                f"packet_no={s['count']}",
                flush=True,
            )

        def cleanup(self) -> None:
            # Base Sink.cleanup() iterates self.audio_data (never populated
            # here since write() doesn't call super().write()) -> harmless
            # no-op either way, but overridden to make that explicit.
            self.finished = True

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.members = True
    bot = discord.Bot(intents=intents)

    @bot.event
    async def on_ready():
        print(f"[bot] logged in as {bot.user} ({bot.user.id})", flush=True)
        guild = bot.get_guild(args.guild_id)
        if guild is None:
            print(f"error: guild {args.guild_id} not found/visible to bot", file=sys.stderr)
            await bot.close()
            return

        channel = guild.get_channel(args.channel_id)
        if channel is None:
            print(f"error: channel {args.channel_id} not found in guild", file=sys.stderr)
            await bot.close()
            return

        print(f"[bot] connecting to voice channel {channel} ...", flush=True)
        vc = await channel.connect()
        print("[bot] connected. starting recording — SPEAK NOW.", flush=True)

        sink = _StreamingSink()

        def _after_recording(exc: Exception | None) -> None:
            if exc is not None:
                print(f"[bot] recording stopped with error: {exc!r}", flush=True)
            else:
                print("[bot] recording stopped cleanly.", flush=True)

        vc.start_recording(sink, _after_recording)

        await asyncio.sleep(args.duration)

        if vc.is_recording():
            vc.stop_recording()
        await vc.disconnect()

        # --- final report ---------------------------------------------------
        print("\n" + "=" * 72)
        print("FINDINGS: per-user packet delivery over the run")
        print("=" * 72)
        if not stats:
            print(
                "NO PACKETS RECEIVED. Either nobody spoke in the channel, "
                "the bot is server-deafened, or receive is broken end-to-end "
                "(check the traceback above / stderr for OpusError, "
                "CryptoError, or MissingVoiceDependenciesError)."
            )
        for user_id, s in stats.items():
            span = (s["last_ts"] - s["first_ts"]) if s["count"] > 1 else 0.0
            first_latency = s["first_ts"] - run_start if s["first_ts"] else None
            print(
                f"user={user_id}: packets={s['count']} bytes={s['bytes']} "
                f"first_packet_latency={first_latency:.3f}s "
                f"span_first_to_last={span:.3f}s"
            )
            print(
                "  -> "
                + (
                    "INCREMENTAL: packets spread across the run (write() fired "
                    "repeatedly over time, not once at stop)."
                    if s["count"] > 5 and span > 1.0
                    else "inconclusive: too few packets to tell — talk longer/more."
                )
            )
        print("=" * 72)

        await bot.close()

    bot.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
