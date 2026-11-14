"""Voice: the owner's voice to dollcore and her voice from dollcore, over `/api/voice` (M6).

One WebSocket `$DOLL_API/api/voice`, opened by the bridge once Discord is ready (same bearer key
as the other bridge endpoints). Binary frames are PCM, text frames are JSON control messages.

bridge → dollcore
- binary: the owner's audio, 16 kHz mono s16le, one frame per received Discord packet (~20 ms),
  only while his packets arrive (nothing during silence / push-to-talk released).
- `hello` (first), `owner_joined`/`owner_left` (the owner's voice channel changes, and
  `owner_joined` right after `hello` when he already is in one), `owner_speaking` (first packet of
  each talk burst: after at least `owner_burst_gap_ms` without his packets), `joined`/`left` (the
  bot's own voice connection), `played` (24 kHz samples of an utterance handed to Discord: every
  `played_interval_ms` while it changes, and once more when the utterance finishes or is
  stopped), `error` (anything that went wrong, including dollcore's protocol violations).

dollcore → bridge
- `join` (the voice channel the owner is in), `leave`, `utterance_start`/`utterance_end`,
  `stop` (drop that utterance's buffered audio now; it is closed: no more audio or
  `utterance_end` for it), `error` (a protocol violation by the bridge, sent right before dollcore
  closes the socket; logged here as ERROR), and binary 24 kHz mono s16le audio of the open
  utterance.

Only the owner is heard: everyone else's packets are dropped before Opus decoding.

A voice failure only affects voice (owner, 2026-11-14). When the socket ends after it once worked
(dollcore closed it, a protocol violation it closed on, the connection was lost), the bridge logs
ERROR, leaves the Discord voice channel, drops its queued audio and player, and opens `/api/voice`
again after `reconnect_delay_s` (each try doubles the next wait, up to `reconnect_max_delay_s`;
a link that then stays up at least `reconnect_max_delay_s` starts over at `reconnect_delay_s`),
starting over with `hello` and the owner's voice state. Discord and the DM stream are not touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, fields
from typing import Any, Awaitable, Callable

import aiohttp
import discord
from discord import opus
from discord.sinks import Sink
from discord.voice.receive import reader as receive_reader

from .audio import HerUpsampler, OwnerDownsampler

log = logging.getLogger("dollbridge.voice")

PROTOCOL_VERSION = 1
#: One Discord frame: 20 ms of 48 kHz stereo s16le.
FRAME_BYTES = opus.Encoder.FRAME_SIZE
SILENCE_FRAME = bytes(FRAME_BYTES)
#: 48 kHz stereo bytes per 24 kHz mono sample handed over (2 output samples × 2 channels × 2 bytes).
OUT_BYTES_PER_IN_SAMPLE = 8


class ProtocolError(Exception):
    """dollcore sent something the contract does not allow."""


class VoiceFatal(Exception):
    """Voice cannot go on (DAVE not ready, UDP keep-alive failing): the bridge ends."""


#: An end of the `/api/voice` connection itself (closed, lost, refused on reconnect): voice
#: reconnects. `CoreAPI.voice` raises ConnectionError when dollcore refuses the handshake.
LINK_ENDED = (OSError, aiohttp.ClientError)


# --- config -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceConfig:
    """`[voice]` in discord.toml; every key is optional and defaults to the value here."""

    #: libopus, loaded explicitly at startup (py-cord does not find Homebrew's on its own).
    opus_library: str = "/opt/homebrew/lib/libopus.dylib"
    #: A packet after at least this long without the owner's packets starts a talk burst.
    owner_burst_gap_ms: int = 200
    #: `played` goes out at most this long apart while an utterance plays (the contract caps it at 100).
    played_interval_ms: int = 100
    #: After the last queued utterance drained, the player sends silence this long before it stops,
    #: so the next sentence starts without restarting it.
    idle_hold_ms: int = 500
    #: Discord's voice connect for `join`.
    connect_timeout_s: float = 15.0
    #: After connecting, DAVE (E2EE) must be ready within this long, else the bridge ends.
    dave_ready_timeout_s: float = 10.0
    #: UDP keep-alive period while receiving (py-cord's own sleeps 5000 s by mistake).
    udp_keepalive_s: float = 5.0
    #: After the link to dollcore ended, wait this long before opening it again. Every try (one
    #: that failed, or one that connected but the link ended again soon) doubles the next wait,
    #: up to `reconnect_max_delay_s`.
    reconnect_delay_s: float = 1.0
    #: The longest wait between reconnect tries. A link that stayed up at least this long starts
    #: over at `reconnect_delay_s` when it ends (a violation loop keeps backing off).
    reconnect_max_delay_s: float = 30.0

    @classmethod
    def from_toml(cls, raw: Any, where: str) -> VoiceConfig:
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: [voice] must be a table")
        known = {f.name: f for f in fields(cls)}
        unknown = set(raw) - set(known)
        if unknown:
            raise ValueError(f"{where}: unknown keys in [voice]: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for name, value in raw.items():
            default = getattr(cls, name)
            if isinstance(default, str):
                ok = isinstance(value, str) and value.strip() != ""
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
            else:
                ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            if not ok:
                raise ValueError(f"{where}: [voice] {name} must be a positive {type(default).__name__}, got {value!r}")
            values[name] = float(value) if isinstance(default, float) else value
        cfg = cls(**values)
        if cfg.played_interval_ms > 100:
            raise ValueError(f"{where}: [voice] played_interval_ms must be at most 100 (the /api/voice contract)")
        if cfg.reconnect_max_delay_s < cfg.reconnect_delay_s:
            raise ValueError(f"{where}: [voice] reconnect_max_delay_s must be at least reconnect_delay_s")
        return cfg


# --- protocol -----------------------------------------------------------------------------------

#: dollcore → bridge message types and the fields each carries besides `type`.
CORE_MESSAGES: dict[str, tuple[str, ...]] = {
    "join": (),
    "leave": (),
    "utterance_start": ("utterance",),
    "error": ("message",),
    "utterance_end": ("utterance",),
    "stop": ("utterance",),
}


@dataclass(frozen=True)
class CoreMessage:
    type: str
    utterance: int | None = None
    message: str | None = None


def decode_core(text: str) -> CoreMessage:
    """One dollcore text frame; anything outside the contract is a ProtocolError."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"text frame is not JSON ({e}): {text[:200]!r}") from None
    if not isinstance(obj, dict):
        raise ProtocolError(f"text frame is not a JSON object: {text[:200]!r}")
    kind = obj.get("type")
    if kind not in CORE_MESSAGES:
        raise ProtocolError(f"unknown message type {kind!r}: {text[:200]!r}")
    expected = {"type", *CORE_MESSAGES[kind]}
    if set(obj) != expected:
        raise ProtocolError(f"{kind} must have exactly the keys {sorted(expected)}, got {sorted(obj)}")
    utterance = obj.get("utterance")
    if "utterance" in expected and (not isinstance(utterance, int) or isinstance(utterance, bool) or utterance < 0):
        raise ProtocolError(f"{kind}: utterance must be a non-negative integer, got {utterance!r}")
    message = obj.get("message")
    if "message" in expected and not isinstance(message, str):
        raise ProtocolError(f"{kind}: message must be a string, got {message!r}")
    return CoreMessage(kind, utterance, message)


def encode(kind: str, **fields: Any) -> str:
    """One bridge → dollcore text frame."""
    return json.dumps({"type": kind, **fields}, ensure_ascii=False)


# --- receive: the owner's voice ------------------------------------------------------------------


class BurstDetector:
    """True for the first packet of a talk burst: the first ever, or after a gap of `gap_s`."""

    def __init__(self, gap_s: float) -> None:
        self.gap_s = gap_s
        self._last: float | None = None

    def packet(self, now: float) -> bool:
        start = self._last is None or now - self._last >= self.gap_s
        self._last = now
        return start


class OwnerSink(Sink):
    """py-cord sink taking Opus packets (`is_opus`) so only the owner's get decoded.

    Runs on py-cord's packet-router thread: `on_audio(pcm16k, burst_start)` and `on_stopped()` must
    be thread-safe. Defines what the PR's `Sink` lacks or gets wrong for receiving (see
    experiments/voice_receive_spike.py): `__sink_listeners__`, `walk_children`, `is_opus`, `write`.
    """

    __sink_listeners__: list[tuple[str, str]] = []

    def __init__(self, owner_id: int, burst_gap_s: float, on_audio: Callable[[bytes, bool], None],
                 on_stopped: Callable[[], None], decoder: Callable[[], Any] = opus.Decoder,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.owner_id = owner_id
        self.on_audio = on_audio
        self.on_stopped = on_stopped
        self._new_decoder = decoder
        self._decoders: dict[int, Any] = {}
        self._burst = BurstDetector(burst_gap_s)
        self._down = OwnerDownsampler()
        self._clock = clock
        #: Set before an intended stop (leave), so `cleanup` does not report it as a failure.
        self.closing = False

    def is_opus(self) -> bool:
        return True

    def walk_children(self):  # noqa: ANN201 — py-cord iterates it
        return iter(())

    def write(self, data: Any, user: Any) -> None:
        if getattr(user, "id", None) != self.owner_id:
            return  # not the owner (or an SSRC not mapped to anyone yet): never decoded
        packet = data.packet
        decoder = self._decoders.get(packet.ssrc)
        if decoder is None:
            decoder = self._decoders[packet.ssrc] = self._new_decoder()
        try:
            pcm = decoder.decode(packet.decrypted_data, fec=False)
        except opus.OpusError as e:
            # What py-cord's own decoder does: conceal the lost frame (PLC), and say so.
            log.warning("owner packet seq=%s did not decode (%s); concealed", packet.sequence, e)
            pcm = decoder.decode(None, fec=False)
        start = self._burst.packet(self._clock())
        if start:
            self._down = OwnerDownsampler()  # a fresh filter: the gap is not part of the signal
        self.on_audio(self._down.process(pcm), start)

    def cleanup(self) -> None:
        self.finished = True
        if not self.closing:
            self.on_stopped()


# --- send: her voice ------------------------------------------------------------------------------


class Utterance:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.up = HerUpsampler()
        self.buf = bytearray()      # 48 kHz stereo s16le, not yet handed to Discord
        self.received = 0           # 24 kHz samples received
        self.out_bytes = 0          # 48 kHz stereo bytes handed to Discord (padding excluded)
        self.ended = False
        self.reported = -1

    @property
    def played(self) -> int:
        """24 kHz samples handed to Discord."""
        return self.out_bytes // OUT_BYTES_PER_IN_SAMPLE


class Speaker(discord.AudioSource):
    """Her utterances, in order, as one py-cord AudioSource.

    The asyncio side calls `start`/`feed`/`end`/`stop`/`abort_all`/`progress`; py-cord's player
    thread calls `read` every 20 ms. A lock guards everything both touch. When the queue is empty
    the player gets silence for `idle_hold_s`, then `b""` (which ends it); `claim_player` tells the
    asyncio side when a new player has to be started, numbered so that the old player's late
    `after` callback cannot mark the new one as ended.
    """

    def __init__(self, idle_hold_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.idle_hold_s = idle_hold_s
        self._clock = clock
        self._lock = threading.Lock()
        self._queue: deque[Utterance] = deque()
        self._open: Utterance | None = None
        self._known: set[int] = set()
        self._finished: list[tuple[int, int]] = []
        self._running = False
        self._player = 0                # number of the newest claimed player
        self._idle_since: float | None = None
        #: 20 ms frames of silence sent because an open utterance had no audio yet (TTS behind).
        self.underruns = 0

    def is_opus(self) -> bool:
        return False

    # asyncio side ---------------------------------------------------------------------------

    def start(self, uid: int) -> None:
        with self._lock:
            if uid in self._known:
                raise ProtocolError(f"utterance_start {uid}: that utterance id was already used")
            if self._open is not None:
                raise ProtocolError(f"utterance_start {uid} while utterance {self._open.id} is still open")
            u = Utterance(uid)
            self._known.add(uid)
            self._open = u
            self._queue.append(u)

    def feed(self, pcm24: bytes) -> None:
        u = self._open
        if u is None:
            raise ProtocolError(f"{len(pcm24)} bytes of audio outside an utterance")
        try:
            out = u.up.process(pcm24)
        except ValueError as e:
            raise ProtocolError(f"utterance {u.id}: {e}") from None
        with self._lock:
            u.buf += out
            u.received += len(pcm24) // 2

    def end(self, uid: int) -> None:
        with self._lock:
            if self._open is None or self._open.id != uid:
                current = "none" if self._open is None else self._open.id
                raise ProtocolError(f"utterance_end {uid}, but the open utterance is {current}")
            self._open.ended = True
            self._open = None

    def stop(self, uid: int) -> int | None:
        """Drops the utterance's buffered audio. Its played samples, or None when it had already
        finished playing (its final `played` already went out)."""
        with self._lock:
            if uid not in self._known:
                raise ProtocolError(f"stop {uid}: no such utterance was started")
            for u in self._queue:
                if u.id == uid:
                    self._queue.remove(u)
                    if self._open is u:
                        self._open = None
                    return u.played
            return None

    def abort_all(self) -> list[tuple[int, int]]:
        """Drops every queued utterance (leaving voice): (id, played) of each, plus finals not yet
        reported."""
        with self._lock:
            out = self._finished + [(u.id, u.played) for u in self._queue]
            self._finished = []
            self._queue.clear()
            self._open = None
            return out

    def progress(self) -> list[tuple[int, int]]:
        """(id, played) to report now: finished utterances' finals, then the playing one if it moved."""
        with self._lock:
            out, self._finished = self._finished, []
            if self._queue:
                head = self._queue[0]
                if head.played != head.reported:
                    head.reported = head.played
                    out.append((head.id, head.played))
            return out

    def claim_player(self) -> int | None:
        """A new player's number when none is reading and there is audio to play (the caller
        starts it), else None."""
        with self._lock:
            if self._running or not any(u.buf or u.ended for u in self._queue):
                return None
            self._running = True
            self._idle_since = None
            self._player += 1
            return self._player

    def player_ended(self, player: int) -> None:
        with self._lock:
            if player == self._player:
                self._running = False

    @property
    def open_utterance(self) -> int | None:
        u = self._open
        return None if u is None else u.id

    # player thread -----------------------------------------------------------------------------

    def read(self) -> bytes:
        with self._lock:
            while self._queue:
                u = self._queue[0]
                if len(u.buf) >= FRAME_BYTES:
                    frame = bytes(u.buf[:FRAME_BYTES])
                    del u.buf[:FRAME_BYTES]
                    u.out_bytes += FRAME_BYTES
                    self._idle_since = None
                    return frame
                if not u.ended:
                    self.underruns += 1
                    self._idle_since = None
                    return SILENCE_FRAME
                self._queue.popleft()
                if u.buf:
                    frame = bytes(u.buf) + bytes(FRAME_BYTES - len(u.buf))
                    u.out_bytes += len(u.buf)
                    u.buf.clear()
                    self._finished.append((u.id, u.played))
                    self._idle_since = None
                    return frame
                self._finished.append((u.id, u.played))
            now = self._clock()
            if self._idle_since is None:
                self._idle_since = now
            if now - self._idle_since >= self.idle_hold_s:
                self._idle_since = None
                self._running = False
                return b""
            return SILENCE_FRAME


# --- macOS: UDP keep-alive ------------------------------------------------------------------------


def install_keepalive_fix(interval_s: float, report: Callable[[str], None]) -> None:
    """Replaces py-cord's receive-side UDP keep-alive (bridge code, not site-packages).

    py-cord's `UDPKeepAlive` calls `sendto()` on the voice socket, which is already connected:
    macOS answers EISCONN, and the thread then spins without waiting. Ours uses `send()`, waits
    `interval_s` between packets, and reports a send failure on a live connection through `report`
    (called on the keep-alive thread), which ends the bridge.
    """

    class SendKeepAlive(receive_reader.UDPKeepAlive):
        def run(self) -> None:
            vc = self.client
            while not self._end_thread.is_set():
                if not vc.wait_until_connected(vc.timeout):
                    if not self._end_thread.is_set():
                        report(f"UDP keep-alive: voice still not connected after {vc.timeout} s")
                    return
                if self._end_thread.is_set():
                    return
                try:
                    vc._connection.socket.send(self.counter.to_bytes(8, "big"))
                except OSError as e:
                    if self._end_thread.is_set() or not vc.is_connected():
                        continue  # disconnecting or reconnecting: wait for the connection again
                    report(f"UDP keep-alive send failed: {e!r}")
                    return
                self.counter = (self.counter + 1) % (1 << 64)
                self._end_thread.wait(interval_s)

    receive_reader.UDPKeepAlive = SendKeepAlive


def load_opus(path: str) -> None:
    """Loads libopus from `path`; ValueError with the reason when it cannot."""
    try:
        opus.load_opus(path)
    except OSError as e:
        raise ValueError(f"cannot load libopus from {path}: {e}") from None
    if not opus.is_loaded():
        raise ValueError(f"libopus at {path} did not load")


# --- the link ---------------------------------------------------------------------------------------


def dave_ready(vc: Any) -> bool:
    session = vc._connection.dave_session
    return session is not None and bool(session.ready)


def owner_voice_channel(client: Any, owner_id: int) -> Any | None:
    """The voice channel the owner is in, in any guild the bot shares with him."""
    for guild in client.guilds:
        state = guild._voice_state_for(owner_id)
        if state is not None and state.channel is not None:
            return state.channel
    return None


class VoiceLink:
    """The `/api/voice` session: dollcore on one side, Discord's voice connection on the other."""

    def __init__(self, cfg: VoiceConfig, owner_id: int, client: Any,
                 connect: Callable[[], Awaitable[Any]], clock: Callable[[], float] = time.monotonic,
                 decoder: Callable[[], Any] = opus.Decoder) -> None:
        self.cfg = cfg
        self.owner_id = owner_id
        self.client = client
        self._connect = connect
        self._clock = clock
        self._decoder = decoder
        self.speaker = Speaker(cfg.idle_hold_ms / 1000, clock)
        self.vc: Any | None = None
        self.sink: OwnerSink | None = None
        self.channel_name: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any | None = None
        self._out: asyncio.Queue[str | bytes] | None = None
        self._fatal: asyncio.Future[None] | None = None
        self._ops = asyncio.Lock()                # join/leave/unexpected disconnects, one at a time
        self._player_idle = asyncio.Event()
        self._player_idle.set()
        # The open utterance refused (not in voice) or cut off (left voice): the rest of its audio
        # and its utterance_end are absorbed; its final `played` already went out.
        self._rejected: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        install_keepalive_fix(cfg.udp_keepalive_s, self._fatal_from_thread)

    # session ---------------------------------------------------------------------------------

    async def run(self) -> None:
        """Runs voice until it hits a fatal error; returns only by raising.

        Fatal: the first connect at startup failing (dollcore unreachable, the key refused,
        `[voice]` disabled in dollcore: nothing ever worked, a setup error) and VoiceFatal (DAVE not
        ready, the UDP keep-alive failing). Any later end of the link is not: it is logged as ERROR
        and the link is opened again (see the module docstring).
        """
        self._loop = asyncio.get_running_loop()
        self._fatal = self._loop.create_future()
        ws = await self._connect()
        delay = self.cfg.reconnect_delay_s
        while True:
            began = self._clock()
            reason = await self._session(ws)
            if self._clock() - began >= self.cfg.reconnect_max_delay_s:
                delay = self.cfg.reconnect_delay_s     # it was up a while: not a failure loop
            log.error("voice: the link to dollcore ended (%s); reconnecting in %.1f s", reason, delay)
            while True:
                await self._wait_or_fatal(delay)
                delay = min(delay * 2, self.cfg.reconnect_max_delay_s)
                try:
                    ws = await self._connect()
                    break
                except LINK_ENDED as e:
                    log.error("voice: reconnecting to /api/voice failed (%s); next try in %.1f s", str(e) or repr(e), delay)
            log.info("voice: reconnected to /api/voice")

    async def _wait_or_fatal(self, seconds: float) -> None:
        assert self._fatal is not None
        done, _ = await asyncio.wait([self._fatal], timeout=seconds)
        if done:
            self._fatal.result()

    async def _session(self, ws: Any) -> str:
        """One connection: `hello`, the owner's voice state, then dollcore's messages until it ends.
        Every connection starts with an empty player. Returns why it ended; raises VoiceFatal."""
        assert self._fatal is not None
        self._ws = ws
        self._out = asyncio.Queue()
        self.speaker = Speaker(self.cfg.idle_hold_ms / 1000, self._clock)
        self._player_idle = asyncio.Event()
        self._player_idle.set()
        self._rejected = None
        self._send(encode("hello", version=PROTOCOL_VERSION))
        channel = owner_voice_channel(self.client, self.owner_id)
        if channel is not None:
            self._send(encode("owner_joined", channel=channel.name))
        parts = [asyncio.create_task(self._receive()), asyncio.create_task(self._sender()),
                 asyncio.create_task(self._reporter())]
        try:
            done, _ = await asyncio.wait([*parts, self._fatal], return_when=asyncio.FIRST_COMPLETED)
            for part in done:
                part.result()
            raise AssertionError("a link part returned")  # each part runs until it raises
        except VoiceFatal as e:
            await self._last_words(str(e))
            raise
        except LINK_ENDED as e:
            return str(e) or repr(e)
        finally:
            await self._end_session(ws, parts)

    async def _end_session(self, ws: Any, parts: list[asyncio.Task[Any]]) -> None:
        """Nothing of this connection goes on: its tasks end, nothing more is sent, and the voice
        channel is left (dollcore forgets it was in one when the socket ends)."""
        self._out = None
        pending = parts + list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.vc is not None:
            vc, self.vc = self.vc, None
            if self.sink is not None:
                self.sink.closing = True
            self.sink = None
            try:
                await vc.disconnect(force=True)
                log.info("voice: left %s because the link to dollcore ended", self.channel_name)
            except Exception as e:  # noqa: BLE001 — the link is ending with its own error
                log.error("voice: leaving %s while the link ended failed: %r", self.channel_name, e)
            self.channel_name = None
        await ws.close()

    async def _receive(self) -> None:
        assert self._ws is not None
        async for m in self._ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                self._on_text(m.data)
            elif m.type == aiohttp.WSMsgType.BINARY:
                self._on_audio(m.data)
            elif m.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError(f"/api/voice failed: {self._ws.exception()}")
        raise ConnectionError(f"dollcore closed /api/voice (code {self._ws.close_code})")

    async def _sender(self) -> None:
        assert self._out is not None and self._ws is not None
        while True:
            item = await self._out.get()
            if isinstance(item, str):
                await self._ws.send_str(item)
            else:
                await self._ws.send_bytes(item)

    async def _reporter(self) -> None:
        every = self.cfg.played_interval_ms / 1000
        while True:
            await asyncio.sleep(every)
            self._report_progress()

    def _report_progress(self) -> None:
        for uid, samples in self.speaker.progress():
            self._send(encode("played", utterance=uid, samples=samples))

    def _send(self, item: str | bytes) -> None:
        if self._out is None:
            what = item if isinstance(item, str) else f"{len(item)} bytes of the owner's audio"
            log.warning("voice: the link to dollcore is down; not sent: %s", what)
            return
        self._out.put_nowait(item)

    def _error(self, message: str) -> None:
        log.error("voice: %s", message)
        self._send(encode("error", message=message))

    async def _last_words(self, message: str) -> None:
        log.error("voice: %s", message)
        try:
            assert self._ws is not None
            await asyncio.wait_for(self._ws.send_str(encode("error", message=message)), 2)
        except Exception as e:  # noqa: BLE001 — the bridge is ending anyway; say why it could not tell
            log.error("voice: could not tell dollcore: %s", e)

    def _fatal_from_thread(self, message: str) -> None:
        loop = self._loop
        if loop is None:
            log.error("voice: %s (before the link ran)", message)
            return
        loop.call_soon_threadsafe(self._set_fatal, message)

    def _set_fatal(self, message: str) -> None:
        if self._fatal is not None and not self._fatal.done():
            self._fatal.set_exception(VoiceFatal(message))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            if t.cancelled():
                return
            e = t.exception()
            if isinstance(e, VoiceFatal):
                self._set_fatal(str(e))
            elif e is not None:
                log.error("voice operation failed", exc_info=e)
                self._error(f"voice operation failed: {e!r}")

        task.add_done_callback(done)

    # dollcore → Discord -------------------------------------------------------------------------

    def _on_text(self, text: str) -> None:
        try:
            m = decode_core(text)
            if m.type == "join":
                self._spawn(self._join())
            elif m.type == "leave":
                self._spawn(self._leave())
            elif m.type == "utterance_start":
                self._utterance_start(m.utterance)
            elif m.type == "utterance_end":
                self._utterance_end(m.utterance)
            elif m.type == "stop":
                self._stop(m.utterance)
            elif m.type == "error":
                # A violation dollcore saw on our side; it closes the socket next, and we reconnect.
                log.error("voice: dollcore reports a protocol violation by the bridge: %s", m.message)
        except ProtocolError as e:
            self._error(f"protocol: {e}")

    def _on_audio(self, data: bytes) -> None:
        if self._rejected is not None and self.speaker.open_utterance is None:
            return  # audio of the refused utterance; refusing it was reported once
        try:
            self.speaker.feed(data)
        except ProtocolError as e:
            self._error(f"protocol: {e}")
            return
        self._kick_player()

    def _utterance_start(self, uid: int) -> None:
        if self._rejected is not None:
            raise ProtocolError(f"utterance_start {uid} while utterance {self._rejected} is still open")
        self.speaker.start(uid)
        if self.vc is None:
            self.speaker.stop(uid)
            self._rejected = uid
            self._error(f"utterance {uid}: not in a voice channel, nothing will be played")
            self._send(encode("played", utterance=uid, samples=0))

    def _utterance_end(self, uid: int) -> None:
        if self._rejected is not None:
            if uid != self._rejected:
                raise ProtocolError(f"utterance_end {uid}, but the open utterance is {self._rejected}")
            self._rejected = None
            return
        self.speaker.end(uid)
        self._kick_player()

    def _stop(self, uid: int) -> None:
        if self._rejected == uid:
            self._rejected = None          # its final `played` already went out
            return
        samples = self.speaker.stop(uid)
        if samples is None:
            log.info("voice: stop %s after it had finished playing", uid)
            return
        self._send(encode("played", utterance=uid, samples=samples))

    def _kick_player(self) -> None:
        if self.vc is None:
            return
        player = self.speaker.claim_player()
        if player is not None:
            self._spawn(self._start_player(self.vc, player))

    async def _start_player(self, vc: Any, player: int) -> None:
        # This connection's speaker and player state: a player of an ended link reports into its
        # own, never into the next connection's.
        speaker, idle = self.speaker, self._player_idle
        await idle.wait()                  # the previous player's thread has finished
        idle.clear()
        try:
            vc.play(speaker, after=lambda exc: self._player_after_thread(speaker, idle, player, exc))
        except Exception as e:  # noqa: BLE001 — told to dollcore; her utterances end here
            speaker.player_ended(player)
            idle.set()
            self._error(f"cannot play: {e!r}")
            self._abort_utterances()

    def _player_after_thread(self, speaker: Speaker, idle: asyncio.Event, player: int,
                             exc: Exception | None) -> None:
        speaker.player_ended(player)
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._player_done, speaker, idle, exc)

    def _player_done(self, speaker: Speaker, idle: asyncio.Event, exc: Exception | None) -> None:
        idle.set()
        if speaker is not self.speaker:
            if exc is not None:
                log.error("voice: playback of an ended link failed: %r", exc)
            return
        if exc is not None:
            self._error(f"playback failed: {exc!r}")
            self._abort_utterances()
        self._kick_player()                # audio that arrived while the player was ending

    def _abort_utterances(self) -> None:
        """Ends every queued utterance with its final `played`. Audio and `utterance_end` still
        coming for the open one are absorbed like a refused utterance's."""
        open_id = self.speaker.open_utterance
        for uid, samples in self.speaker.abort_all():
            self._send(encode("played", utterance=uid, samples=samples))
        if open_id is not None:
            self._rejected = open_id

    async def _join(self) -> None:
        async with self._ops:
            if self.vc is not None:
                self._error(f"join: already in voice channel {self.channel_name}")
                return
            channel = owner_voice_channel(self.client, self.owner_id)
            if channel is None:
                self._error("join: the owner is not in a voice channel")
                return
            try:
                vc = await channel.connect(timeout=self.cfg.connect_timeout_s)
            except Exception as e:  # noqa: BLE001 — she is told; she can try again
                self._error(f"join: cannot connect to voice channel {channel.name}: {e!r}")
                return
            deadline = self._clock() + self.cfg.dave_ready_timeout_s
            while not dave_ready(vc):
                if self._clock() >= deadline:
                    await vc.disconnect(force=True)
                    raise VoiceFatal(f"DAVE (voice E2EE) not ready {self.cfg.dave_ready_timeout_s} s after "
                                     f"joining {channel.name}: the owner's voice cannot be decrypted")
                await asyncio.sleep(0.05)
            sink = OwnerSink(self.owner_id, self.cfg.owner_burst_gap_ms / 1000, self._owner_audio_thread,
                             self._receive_stopped_thread, decoder=self._decoder)
            vc.start_recording(sink)
            self.vc, self.sink, self.channel_name = vc, sink, channel.name
            log.info("voice: joined %s", channel.name)
            self._send(encode("joined", channel=channel.name))

    async def _leave(self) -> None:
        async with self._ops:
            if self.vc is None:
                self._error("leave: not in a voice channel")
                return
            await self._disconnect()

    async def _disconnect(self) -> None:
        vc, sink = self.vc, self.sink
        assert vc is not None
        self.vc = self.sink = None
        if sink is not None:
            sink.closing = True
        self._abort_utterances()
        await vc.disconnect(force=True)
        log.info("voice: left %s", self.channel_name)
        self.channel_name = None
        self._send(encode("left"))

    # Discord → dollcore -----------------------------------------------------------------------

    def _owner_audio_thread(self, pcm16k: bytes, burst_start: bool) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._owner_audio, pcm16k, burst_start)

    def _owner_audio(self, pcm16k: bytes, burst_start: bool) -> None:
        if burst_start:
            self._send(encode("owner_speaking"))
        if pcm16k:
            self._send(pcm16k)

    def _receive_stopped_thread(self) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._receive_stopped)

    def _receive_stopped(self) -> None:
        if self.vc is None:
            return
        reader = getattr(self.vc, "_reader", None)
        reason = getattr(reader, "error", None)
        self._error(f"voice receive stopped unexpectedly ({reason!r}); leaving {self.channel_name}")
        self._spawn(self._leave_after_failure())

    async def _leave_after_failure(self) -> None:
        async with self._ops:
            if self.vc is not None:
                await self._disconnect()

    def on_voice_state(self, member: Any, before: Any, after: Any) -> None:
        """discord's `on_voice_state_update`, forwarded by the bridge."""
        if self._out is None:
            return  # not linked (yet, or again): the next `hello` carries the owner's state
        was = before.channel.id if before.channel is not None else None
        now = after.channel.id if after.channel is not None else None
        if was == now:
            return  # mute/deafen/stream changes
        if member.id == self.owner_id:
            if was is not None:
                self._send(encode("owner_left"))
            if now is not None:
                self._send(encode("owner_joined", channel=after.channel.name))
        elif self.client.user is not None and member.id == self.client.user.id and self.vc is not None:
            if now is None:
                log.warning("voice: disconnected from %s by Discord", self.channel_name)
                self._spawn(self._leave_after_failure())
            elif was is not None:
                self.channel_name = after.channel.name
                self._send(encode("joined", channel=after.channel.name))
