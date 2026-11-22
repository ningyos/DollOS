"""Voice (M6): protocol, resampling, burst detection, play accounting, the keep-alive fix, and the
whole `/api/voice` link against a fake dollcore WebSocket server and fake Discord voice objects."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace as NS
from typing import Any

import aiohttp
import numpy as np
import pytest
from aiohttp import web
from discord import opus
from discord.voice.receive import reader as receive_reader

from dollbridge.api import CoreAPI
from dollbridge.core import Bridge, pump
from dollbridge.audio import HerUpsampler, OwnerDownsampler
from dollbridge.voice import (
    FRAME_BYTES,
    SILENCE_FRAME,
    BurstDetector,
    CoreMessage,
    OwnerSink,
    ProtocolError,
    Speaker,
    VoiceConfig,
    VoiceFatal,
    VoiceLink,
    decode_core,
    encode,
    install_keepalive_fix,
)

OWNER, OTHER, BOT = 111, 222, 999


# --- protocol -------------------------------------------------------------------------------------


def test_decode_core_messages() -> None:
    assert decode_core('{"type":"join"}') == CoreMessage("join")
    assert decode_core('{"type":"leave"}') == CoreMessage("leave")
    assert decode_core('{"type":"utterance_start","utterance":3}') == CoreMessage("utterance_start", 3)
    assert decode_core('{"type":"utterance_end","utterance":3}') == CoreMessage("utterance_end", 3)
    assert decode_core('{"type":"stop","utterance":0}') == CoreMessage("stop", 0)
    assert decode_core('{"type":"error","message":"hello twice"}') == CoreMessage("error", None, "hello twice")


@pytest.mark.parametrize("bad", [
    "not json", "[1]", '{"type":"dance"}', '{"utterance":1}', '{"type":"join","x":1}',
    '{"type":"stop"}', '{"type":"stop","utterance":"1"}', '{"type":"stop","utterance":true}',
    '{"type":"stop","utterance":-1}', '{"type":"utterance_start","utterance":1.5}',
    '{"type":"error"}', '{"type":"error","message":3}', '{"type":"error","message":"x","utterance":1}',
])
def test_decode_core_rejects(bad: str) -> None:
    with pytest.raises(ProtocolError):
        decode_core(bad)


def test_encode() -> None:
    assert json.loads(encode("owner_joined", channel="語音")) == {"type": "owner_joined", "channel": "語音"}
    assert "語音" in encode("owner_joined", channel="語音")  # not \\u-escaped
    assert json.loads(encode("played", utterance=2, samples=480)) == {"type": "played", "utterance": 2, "samples": 480}


# --- config ---------------------------------------------------------------------------------------


def test_voice_config() -> None:
    assert VoiceConfig.from_toml({}, "x") == VoiceConfig()
    c = VoiceConfig.from_toml({"owner_burst_gap_ms": 250, "connect_timeout_s": 20, "opus_library": "/x.dylib"}, "x")
    assert (c.owner_burst_gap_ms, c.connect_timeout_s, c.opus_library) == (250, 20.0, "/x.dylib")
    assert isinstance(c.connect_timeout_s, float)
    for bad in [{"nope": 1}, {"owner_burst_gap_ms": 0}, {"owner_burst_gap_ms": 1.5}, {"owner_burst_gap_ms": True},
                {"played_interval_ms": 101}, {"opus_library": ""}, {"udp_keepalive_s": "5"}, [],
                {"reconnect_delay_s": 0}, {"reconnect_delay_s": 5, "reconnect_max_delay_s": 2}]:
        with pytest.raises(ValueError):
            VoiceConfig.from_toml(bad, "x")


# --- resampling -----------------------------------------------------------------------------------


def tone(freq: float, rate: int, seconds: float, amp: float = 10000.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def pcm(x: np.ndarray) -> bytes:
    return np.rint(x).astype("<i2").tobytes()


def samples(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float64)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)))


def stereo(x: np.ndarray) -> bytes:
    return pcm(np.repeat(x, 2))


def test_owner_downsampler_lengths_and_chunking() -> None:
    x = tone(440, 48000, 0.5)
    whole = OwnerDownsampler().process(stereo(x))
    d = OwnerDownsampler()
    frames = [d.process(stereo(x[i:i + 960])) for i in range(0, len(x), 960)]
    assert all(len(f) == 640 for f in frames)        # 20 ms at 16 kHz mono = 320 samples
    assert b"".join(frames) == whole
    assert len(whole) == len(x) // 3 * 2


def test_owner_downsampler_keeps_speech_band_and_removes_alias() -> None:
    kept = samples(OwnerDownsampler().process(stereo(tone(1000, 48000, 0.5))))[100:]
    assert abs(rms(kept) / (10000 / np.sqrt(2)) - 1) < 0.02
    # 10 kHz is above 16 kHz's Nyquist: without the filter it would fold to 6 kHz at full level.
    folded = samples(OwnerDownsampler().process(stereo(tone(10000, 48000, 0.5))))[100:]
    assert rms(folded) < 0.05 * 10000 / np.sqrt(2)


def test_owner_downsampler_averages_channels() -> None:
    left, right = tone(1000, 48000, 0.2), np.zeros(9600)
    interleaved = pcm(np.column_stack([left, right]).reshape(-1))
    out = samples(OwnerDownsampler().process(interleaved))[100:]
    assert abs(rms(out) / (5000 / np.sqrt(2)) - 1) < 0.03


def test_her_upsampler() -> None:
    x = tone(1000, 24000, 0.5)
    whole = HerUpsampler().process(pcm(x))
    u = HerUpsampler()
    chunks = [u.process(pcm(x[i:i + 480])) for i in range(0, len(x), 480)]
    assert all(len(c) == FRAME_BYTES for c in chunks)  # 480 samples at 24 kHz = one 20 ms Discord frame
    assert b"".join(chunks) == whole
    out = samples(whole).reshape(-1, 2)
    assert np.array_equal(out[:, 0], out[:, 1])
    assert abs(rms(out[200:, 0]) / (10000 / np.sqrt(2)) - 1) < 0.02


def test_odd_byte_counts_are_errors() -> None:
    with pytest.raises(ValueError):
        HerUpsampler().process(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        OwnerDownsampler().process(b"\x00\x01")    # one sample, not a stereo frame


def test_empty_chunks_resample_to_nothing() -> None:
    # voice-mlx closes a sentence with an empty final chunk when every frame was already streamed;
    # it reached the bridge as an empty binary frame and used to come out as one extra sample.
    x = tone(1000, 24000, 0.1)
    u = HerUpsampler()
    assert u.process(b"") == b""
    first = u.process(pcm(x[:1920]))
    assert u.process(b"") == b""
    assert first + u.process(pcm(x[1920:])) == HerUpsampler().process(pcm(x))
    d = OwnerDownsampler()
    assert d.process(b"") == b""
    d.process(stereo(tone(1000, 48000, 0.1)))
    assert d.process(b"") == b""


def test_empty_audio_frames_are_not_played() -> None:
    # 2026-11-14 live: "played: 80641 samples of utterance 2, only 80640 sent".
    s = Speaker(idle_hold_s=0.5)
    s.start(2)
    for _ in range(42):
        s.feed(her(1920))
        s.feed(b"")
    s.end(2)
    while s.read() not in (SILENCE_FRAME, b""):
        pass
    assert s.progress() == [(2, 80640)]


# --- bursts and the owner-only sink -------------------------------------------------------------------


def test_burst_detector() -> None:
    b = BurstDetector(0.2)
    assert [b.packet(t) for t in (0.0, 0.02, 0.04, 0.23, 0.25, 0.449, 0.65)] == [
        True, False, False, False, False, False, True]
    exact = BurstDetector(0.25)                      # binary-exact times: a gap of exactly the
    assert exact.packet(1.0) and not exact.packet(1.125) and exact.packet(1.375)   # gap starts a burst


class FakeDecoder:
    def __init__(self, fail_on: bytes | None = None) -> None:
        self.calls: list[bytes | None] = []
        self.fail_on = fail_on

    def decode(self, data: bytes | None, *, fec: bool) -> bytes:
        self.calls.append(data)
        if data is not None and data == self.fail_on:
            raise opus.OpusError(-4, "corrupted stream")
        return bytes(960 * 4)                        # 20 ms of 48 kHz stereo silence


def voice_packet(ssrc: int = 7, data: bytes = b"opus", seq: int = 1) -> Any:
    return NS(packet=NS(ssrc=ssrc, sequence=seq, decrypted_data=data))


def make_sink(clock: list[float], fail_on: bytes | None = None) -> tuple[OwnerSink, list[Any], list[FakeDecoder], list[int]]:
    got: list[Any] = []
    decoders: list[FakeDecoder] = []
    stopped: list[int] = []

    def new_decoder() -> FakeDecoder:
        decoders.append(FakeDecoder(fail_on))
        return decoders[-1]

    sink = OwnerSink(OWNER, 0.2, lambda b, s: got.append((len(b), s)), lambda: stopped.append(1),
                     decoder=new_decoder, clock=lambda: clock[0])
    return sink, got, decoders, stopped


def test_sink_decodes_only_the_owner() -> None:
    clock = [0.0]
    sink, got, decoders, _ = make_sink(clock)
    assert sink.is_opus() and list(sink.walk_children()) == []
    sink.write(voice_packet(ssrc=8), NS(id=OTHER))
    sink.write(voice_packet(ssrc=9), None)            # SSRC not mapped to a user yet
    assert decoders == [] and got == []
    for t in (0.0, 0.02, 0.5):
        clock[0] = t
        sink.write(voice_packet(), NS(id=OWNER))
    assert got == [(640, True), (640, False), (640, True)]
    assert len(decoders) == 1 and decoders[0].calls == [b"opus"] * 3


def test_sink_conceals_undecodable_packets() -> None:
    sink, got, decoders, _ = make_sink([0.0], fail_on=b"bad")
    sink.write(voice_packet(data=b"bad"), NS(id=OWNER))
    assert decoders[0].calls == [b"bad", None] and got == [(640, True)]


def test_sink_reports_unexpected_stop_only() -> None:
    sink, _, _, stopped = make_sink([0.0])
    sink.cleanup()
    assert stopped == [1]
    sink.closing = True
    sink.cleanup()
    assert stopped == [1]


# --- the speaker: play accounting and stop --------------------------------------------------------------


def her(n: int) -> bytes:
    return pcm(tone(300, 24000, n / 24000))


def test_play_position_accounting() -> None:
    now = [0.0]
    s = Speaker(idle_hold_s=0.5, clock=lambda: now[0])
    assert s.claim_player() is None                   # nothing to play
    s.start(1)
    assert s.claim_player() is None                   # open but no audio yet
    s.feed(her(1000))                                 # 8000 bytes at 48 kHz stereo
    assert s.claim_player() == 1 and s.claim_player() is None   # one player at a time
    assert len(s.read()) == FRAME_BYTES and len(s.read()) == FRAME_BYTES
    assert s.progress() == [(1, 960)] and s.progress() == []
    assert s.read() == SILENCE_FRAME and s.underruns == 1   # 320 bytes left, utterance still open
    s.end(1)
    last = s.read()
    assert len(last) == FRAME_BYTES and last[320:] == bytes(FRAME_BYTES - 320)
    assert s.progress() == [(1, 1000)]               # the final: every received sample
    assert s.read() == SILENCE_FRAME                  # idle hold
    now[0] = 0.49
    assert s.read() == SILENCE_FRAME
    now[0] = 0.5
    assert s.read() == b""                            # the player ends
    assert s.claim_player() is None


def test_a_late_after_does_not_end_the_next_player() -> None:
    s = Speaker(idle_hold_s=0.0, clock=lambda: 0.0)
    s.start(1)
    s.feed(her(480))
    s.end(1)
    first = s.claim_player()
    s.read()
    assert s.read() == b""                            # player 1 ends by itself...
    s.start(2)
    s.feed(her(480))
    second = s.claim_player()
    assert second == 2
    s.player_ended(first)                             # ...and its `after` arrives late
    assert s.claim_player() is None                   # player 2 still counts as running
    s.player_ended(second)
    assert s.claim_player() == 3


def test_utterances_play_back_to_back() -> None:
    s = Speaker(idle_hold_s=0.5, clock=lambda: 0.0)
    s.start(1)
    s.feed(her(960))                                  # exactly two frames
    s.end(1)
    s.start(2)
    s.feed(her(480))
    frames = [s.read() for _ in range(3)]
    assert SILENCE_FRAME not in frames
    assert s.progress() == [(1, 960), (2, 480)]


def test_stop_drops_the_buffer_now() -> None:
    s = Speaker(idle_hold_s=0.5, clock=lambda: 0.0)
    s.start(4)
    s.feed(her(4800))
    s.read()
    assert s.stop(4) == 480                           # one frame went to Discord
    assert s.read() == SILENCE_FRAME and s.progress() == []
    with pytest.raises(ProtocolError):
        s.feed(her(480))                              # stopped = closed
    with pytest.raises(ProtocolError):
        s.end(4)
    assert s.stop(4) is None                          # already gone: not an error
    with pytest.raises(ProtocolError):
        s.stop(5)                                     # never started


def test_stop_after_finishing_is_a_no_op() -> None:
    s = Speaker(idle_hold_s=0.5, clock=lambda: 0.0)
    s.start(1)
    s.feed(her(480))
    s.end(1)
    s.read()
    s.read()
    assert s.progress() == [(1, 480)]
    assert s.stop(1) is None


def test_speaker_protocol_errors() -> None:
    s = Speaker(idle_hold_s=0.5)
    with pytest.raises(ProtocolError):
        s.feed(her(10))
    with pytest.raises(ProtocolError):
        s.end(1)
    s.start(1)
    with pytest.raises(ProtocolError):
        s.start(2)                                    # 1 is still open
    with pytest.raises(ProtocolError):
        s.end(2)
    with pytest.raises(ProtocolError):
        s.feed(b"\x01")                               # half a sample
    s.end(1)
    with pytest.raises(ProtocolError):
        s.start(1)                                    # ids are not reused


def test_abort_all() -> None:
    s = Speaker(idle_hold_s=0.5, clock=lambda: 0.0)
    s.start(1)
    s.feed(her(960))
    s.end(1)
    s.read(), s.read()
    s.start(2)
    s.feed(her(960))
    s.read()
    assert s.abort_all() == [(1, 960), (2, 480)]
    assert s.open_utterance is None and s.progress() == []


# --- the keep-alive fix ------------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, fail: OSError | None = None) -> None:
        self.sent: list[bytes] = []
        self.fail = fail

    def send(self, data: bytes) -> int:
        if self.fail:
            raise self.fail
        self.sent.append(data)
        return len(data)

    def sendto(self, data: bytes, addr: Any) -> int:
        raise OSError(56, "Socket is already connected")   # what macOS says


def keepalive_client(sock: FakeSocket) -> Any:
    return NS(timeout=1.0, wait_until_connected=lambda timeout=None: True, is_connected=lambda: True,
              _connection=NS(socket=sock))


@pytest.fixture
def restore_keepalive() -> Any:
    original = receive_reader.UDPKeepAlive
    yield
    receive_reader.UDPKeepAlive = original


def test_keepalive_uses_send(restore_keepalive: Any) -> None:
    reports: list[str] = []
    install_keepalive_fix(0.01, reports.append)
    sock = FakeSocket()
    ka = receive_reader.UDPKeepAlive(keepalive_client(sock))
    ka.start()
    # Wait for three sends (every 0.01 s) rather than a fixed 0.1 s: a slow CI machine sent only two in that time.
    deadline = time.monotonic() + 5
    while len(sock.sent) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    ka.stop()
    ka.join(1)
    assert not ka.is_alive() and reports == []
    assert len(sock.sent) >= 3 and sock.sent[:2] == [(0).to_bytes(8, "big"), (1).to_bytes(8, "big")]


def test_keepalive_failure_is_reported(restore_keepalive: Any) -> None:
    reports: list[str] = []
    install_keepalive_fix(0.01, reports.append)
    ka = receive_reader.UDPKeepAlive(keepalive_client(FakeSocket(OSError(65, "No route to host"))))
    ka.start()
    ka.join(1)
    assert len(reports) == 1 and "No route to host" in reports[0]


# --- the whole link against a fake dollcore and fake Discord ----------------------------------------------------


class FakeVC:
    """Discord's VoiceClient as the link uses it; the player is driven by the test."""

    def __init__(self, dave_ready: bool = True) -> None:
        self._connection = NS(dave_session=NS(ready=dave_ready))
        self.sink: OwnerSink | None = None
        self.source: Any = None
        self.after: Any = None
        self.plays = 0
        self.disconnected = False

    def start_recording(self, sink: OwnerSink) -> None:
        self.sink = sink

    def play(self, source: Any, *, after: Any) -> None:
        self.source, self.after = source, after
        self.plays += 1

    def pump(self, frames: int) -> list[bytes]:
        return [self.source.read() for _ in range(frames)]

    def finish(self) -> None:
        """What py-cord's player does: read until b"" (on its own thread), then `after`."""
        def run() -> None:
            while self.source.read():
                time.sleep(0.001)
            self.after(None)
        t = threading.Thread(target=run)
        t.start()
        t.join(2)

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnected = True


class FakeChannel:
    def __init__(self, name: str, vc: FakeVC) -> None:
        self.id, self.name, self.vc = hash(name), name, vc
        self.connects = 0

    async def connect(self, *, timeout: float) -> FakeVC:
        self.connects += 1
        return self.vc


class FakeGuild:
    def __init__(self) -> None:
        self.states: dict[int, Any] = {}

    def _voice_state_for(self, user_id: int) -> Any:
        return self.states.get(user_id)


class Dollcore:
    """A fake dollcore: the `/api/voice` server side, scripted by the test, plus the DM side
    (`/api/listen` lines from `lines`, `/api/owner` posts into `posted`)."""

    def __init__(self) -> None:
        self.ws: web.WebSocketResponse | None = None
        self.connected = asyncio.Event()
        self.connections = 0
        self.refuse = 0                    # this many next `/api/voice` handshakes get 409
        self.texts: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.binary: list[bytes] = []
        self.order: list[str] = []
        self.lines: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.posted: list[str] = []

    async def listen(self, req: web.Request) -> web.StreamResponse:
        r = web.StreamResponse()
        await r.prepare(req)
        await r.write(b'{"type":"hello"}\n')
        while (line := await self.lines.get()) is not None:
            await r.write((json.dumps(line) + "\n").encode())
        return r

    async def owner(self, req: web.Request) -> web.Response:
        self.posted.append((await req.json())["text"])
        return web.json_response({"queued": True})

    async def handler(self, req: web.Request) -> web.StreamResponse:
        if req.headers.get("Authorization") != "Bearer k":
            return web.json_response({"error": "owner key required"}, status=401)
        if self.refuse:
            self.refuse -= 1
            return web.json_response({"error": "a voice connection is already open"}, status=409)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws = ws
        self.connections += 1
        self.connected.set()
        async for m in ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                msg = json.loads(m.data)
                self.order.append(msg["type"])
                await self.texts.put(msg)
            elif m.type == aiohttp.WSMsgType.BINARY:
                self.order.append("audio")
                self.binary.append(m.data)
        return ws

    async def expect(self, kind: str, skip: tuple[str, ...] = ("played",)) -> dict[str, Any]:
        while True:
            msg = await asyncio.wait_for(self.texts.get(), 2)
            if msg["type"] == kind:
                return msg
            assert msg["type"] in skip, f"expected {kind}, got {msg}"

    async def played_until(self, uid: int, samples: int) -> list[int]:
        seen: list[int] = []
        while True:
            msg = await self.expect("played", skip=())
            # A progress report of the utterance before can still arrive late (it reports every
            # <= 100 ms): skip those, but nothing of a later one may come first.
            if msg["utterance"] < uid:
                continue
            assert msg["utterance"] == uid
            seen.append(msg["samples"])
            if msg["samples"] == samples:
                return seen

    async def send(self, **msg: Any) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(msg))


@pytest.fixture
async def rig(restore_keepalive: Any) -> Any:
    core = Dollcore()
    app = web.Application()
    app.router.add_get("/api/voice", core.handler)
    app.router.add_get("/api/listen", core.listen)
    app.router.add_post("/api/owner", core.owner)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    session = aiohttp.ClientSession()
    guild = FakeGuild()
    client = NS(guilds=[guild], user=NS(id=BOT))
    api = CoreAPI(f"http://127.0.0.1:{port}", "k", session)
    cfg = VoiceConfig(idle_hold_ms=1, played_interval_ms=20, dave_ready_timeout_s=0.2,
                      reconnect_delay_s=0.05, reconnect_max_delay_s=0.2)
    link = VoiceLink(cfg, OWNER, client, api.voice, decoder=FakeDecoder)
    rig = NS(core=core, guild=guild, link=link, api=api, session=session, task=None)
    yield rig
    if rig.task is not None and not rig.task.done():
        rig.task.cancel()
        await asyncio.gather(rig.task, return_exceptions=True)
    await core.lines.put(None)             # ends a DM stream a test left open
    await session.close()
    await runner.cleanup()


def owner_in(rig: Any, name: str) -> FakeChannel:
    vc = FakeVC()
    channel = FakeChannel(name, vc)
    rig.guild.states[OWNER] = NS(channel=channel)
    return channel


def state(channel: Any) -> Any:
    return NS(channel=channel)


async def start(rig: Any) -> None:
    rig.task = asyncio.create_task(rig.link.run())
    await asyncio.wait_for(rig.core.connected.wait(), 2)
    assert await rig.core.expect("hello") == {"type": "hello", "version": 1}


async def test_link_join_hear_speak_stop_leave(rig: Any) -> None:
    channel = owner_in(rig, "客廳")
    await start(rig)
    assert await rig.core.expect("owner_joined") == {"type": "owner_joined", "channel": "客廳"}

    await rig.core.send(type="join")
    assert await rig.core.expect("joined") == {"type": "joined", "channel": "客廳"}
    vc = channel.vc
    assert vc.sink is not None

    # Hearing: only the owner, as 16 kHz frames, `owner_speaking` before the first of a burst.
    sink = vc.sink
    t = threading.Thread(target=lambda: [sink.write(voice_packet(ssrc=s), NS(id=u))
                                         for s, u in ((1, OTHER), (7, OWNER), (7, OWNER))])
    t.start()
    t.join()
    assert await rig.core.expect("owner_speaking") == {"type": "owner_speaking"}
    for _ in range(50):
        if len(rig.core.binary) == 2:
            break
        await asyncio.sleep(0.01)
    assert [len(b) for b in rig.core.binary] == [640, 640]
    assert rig.core.order[-3:] == ["owner_speaking", "audio", "audio"]

    # Speaking: utterance 1 plays to the end; `played` climbs to its total.
    await rig.core.send(type="utterance_start", utterance=1)
    await rig.core.ws.send_bytes(her(2400))
    await rig.core.send(type="utterance_end", utterance=1)
    for _ in range(50):
        if vc.plays:
            break
        await asyncio.sleep(0.01)
    assert vc.plays == 1
    vc.pump(2)
    assert (await rig.core.played_until(1, 960))[-1] == 960
    await asyncio.to_thread(vc.finish)
    assert (await rig.core.played_until(1, 2400))[-1] == 2400

    # Interrupted: utterance 2 stops after one frame; the final `played` says so.
    await rig.core.send(type="utterance_start", utterance=2)
    await rig.core.ws.send_bytes(her(4800))
    for _ in range(50):
        if vc.plays == 2:
            break
        await asyncio.sleep(0.01)
    assert vc.plays == 2                               # a new player after the first one ended
    vc.pump(1)
    await rig.core.send(type="stop", utterance=2)
    assert (await rig.core.played_until(2, 480))[-1] == 480
    assert vc.pump(1) == [SILENCE_FRAME]               # nothing of utterance 2 left

    # Protocol violations are answered with an error, not dropped.
    await rig.core.ws.send_bytes(her(480))             # no open utterance
    assert "outside an utterance" in (await rig.core.expect("error"))["message"]
    await rig.core.send(type="dance")
    assert "unknown message type" in (await rig.core.expect("error"))["message"]

    await rig.core.send(type="leave")
    assert await rig.core.expect("left") == {"type": "left"}
    assert vc.disconnected and sink.closing
    await rig.core.send(type="leave")
    assert "not in a voice channel" in (await rig.core.expect("error"))["message"]


async def test_link_owner_and_bot_voice_states(rig: Any) -> None:
    await start(rig)                                   # owner not in voice: no owner_joined
    a, b = NS(id=1, name="A"), NS(id=2, name="B")
    link = rig.link
    link.on_voice_state(NS(id=OTHER), state(None), state(a))      # someone else
    link.on_voice_state(NS(id=OWNER), state(None), state(a))
    assert await rig.core.expect("owner_joined", skip=()) == {"type": "owner_joined", "channel": "A"}
    link.on_voice_state(NS(id=OWNER), state(a), state(a))         # mute/deafen: no change
    link.on_voice_state(NS(id=OWNER), state(a), state(b))         # moved
    assert await rig.core.expect("owner_left", skip=()) == {"type": "owner_left"}
    assert await rig.core.expect("owner_joined", skip=()) == {"type": "owner_joined", "channel": "B"}
    link.on_voice_state(NS(id=OWNER), state(b), state(None))
    assert await rig.core.expect("owner_left", skip=()) == {"type": "owner_left"}

    await rig.core.send(type="join")                   # the owner is not in voice now
    assert "owner is not in a voice channel" in (await rig.core.expect("error", skip=()))["message"]

    channel = owner_in(rig, "C")
    await rig.core.send(type="join")
    await rig.core.expect("joined", skip=())
    await rig.core.send(type="join")
    assert "already in voice channel C" in (await rig.core.expect("error", skip=()))["message"]
    link.on_voice_state(NS(id=BOT), state(channel), state(b))     # moved by someone
    assert await rig.core.expect("joined", skip=()) == {"type": "joined", "channel": "B"}
    link.on_voice_state(NS(id=BOT), state(b), state(None))        # kicked
    assert await rig.core.expect("left", skip=()) == {"type": "left"}
    assert channel.vc.disconnected


async def test_link_utterance_while_not_in_voice(rig: Any) -> None:
    await start(rig)
    await rig.core.send(type="utterance_start", utterance=1)
    assert "not in a voice channel" in (await rig.core.expect("error", skip=()))["message"]
    assert await rig.core.expect("played", skip=()) == {"type": "played", "utterance": 1, "samples": 0}
    await rig.core.ws.send_bytes(her(480))             # its audio: refused once, already reported
    await rig.core.send(type="utterance_end", utterance=1)
    await rig.core.send(type="utterance_end", utterance=1)
    assert "open utterance is none" in (await rig.core.expect("error", skip=()))["message"]


async def test_link_leave_mid_utterance(rig: Any) -> None:
    channel = owner_in(rig, "客廳")
    await start(rig)
    await rig.core.send(type="join")
    await rig.core.expect("joined", skip=("owner_joined",))
    await rig.core.send(type="utterance_start", utterance=1)
    await rig.core.ws.send_bytes(her(960))
    for _ in range(50):
        if channel.vc.plays:
            break
        await asyncio.sleep(0.01)
    channel.vc.pump(1)
    await rig.core.send(type="leave")
    assert (await rig.core.played_until(1, 480))[-1] == 480     # what was heard
    assert await rig.core.expect("left", skip=()) == {"type": "left"}
    await rig.core.ws.send_bytes(her(480))             # still in flight from dollcore: absorbed
    await rig.core.send(type="utterance_end", utterance=1)
    await rig.core.send(type="utterance_start", utterance=2)  # not in voice any more
    assert "utterance 2: not in a voice channel" in (await rig.core.expect("error", skip=()))["message"]


async def test_link_unexpected_receive_stop(rig: Any) -> None:
    channel = owner_in(rig, "客廳")
    await start(rig)
    await rig.core.send(type="join")
    await rig.core.expect("joined", skip=("owner_joined",))
    await asyncio.to_thread(channel.vc.sink.cleanup)   # py-cord's reader died
    assert "voice receive stopped" in (await rig.core.expect("error", skip=()))["message"]
    assert await rig.core.expect("left", skip=()) == {"type": "left"}


async def test_link_dave_not_ready_ends_the_bridge(rig: Any) -> None:
    channel = owner_in(rig, "客廳")
    channel.vc._connection.dave_session = NS(ready=False)
    await start(rig)
    await rig.core.send(type="join")
    with pytest.raises(VoiceFatal, match="DAVE"):
        await asyncio.wait_for(rig.task, 2)
    assert "DAVE" in (await rig.core.expect("error", skip=("owner_joined",)))["message"]
    assert channel.vc.disconnected


async def test_link_keepalive_failure_ends_the_bridge(rig: Any) -> None:
    await start(rig)
    rig.link._fatal_from_thread("UDP keep-alive send failed: boom")
    with pytest.raises(VoiceFatal, match="keep-alive"):
        await asyncio.wait_for(rig.task, 2)


class FakeDM:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


async def until(cond: Any, seconds: float = 2) -> None:
    for _ in range(int(seconds / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def test_link_survives_dollcore_closing_mid_utterance(rig: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Owner, 2026-11-14: a voice failure only affects voice. The socket ends mid-utterance: the
    bridge leaves the channel, reconnects with `hello` and the owner's state, the DM path keeps
    working, and she can join and speak again with a fresh player."""
    channel = owner_in(rig, "客廳")
    dms = Bridge(OWNER, rig.api, self_id=lambda: BOT, attach=lambda path: path)
    dms.dm = dm = FakeDM()
    dm_stream = asyncio.create_task(pump(rig.api.listen, dms.on_core))
    await start(rig)
    await rig.core.expect("owner_joined")
    await rig.core.send(type="join")
    await rig.core.expect("joined")
    vc1 = channel.vc
    await rig.core.send(type="utterance_start", utterance=1)
    await rig.core.ws.send_bytes(her(4800))
    await until(lambda: vc1.plays == 1)
    vc1.pump(1)
    await rig.core.played_until(1, 480)

    rig.core.connected.clear()
    await rig.core.ws.close()                          # dollcore ends the link mid-utterance
    await asyncio.wait_for(rig.core.connected.wait(), 2)
    assert rig.core.connections == 2
    assert await rig.core.expect("hello") == {"type": "hello", "version": 1}
    assert await rig.core.expect("owner_joined", skip=()) == {"type": "owner_joined", "channel": "客廳"}
    assert vc1.disconnected and vc1.sink.closing and rig.link.vc is None and not rig.task.done()
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "link to dollcore ended" in r.getMessage()]
    assert len(errors) == 1 and "dollcore closed /api/voice" in errors[0].getMessage()

    # The DM path never noticed: her line reaches the DM, the owner's reaches dollcore.
    await rig.core.lines.put({"type": "say", "text": "還在喔"})
    await until(lambda: dm.sent == ["還在喔"])
    await dms.on_message(NS(author=NS(id=OWNER), content="語音怎麼了", guild=None, channel=NS(id=5), attachments=[]))
    assert rig.core.posted == ["語音怎麼了"]
    assert not dm_stream.done()

    # Rejoin and speak again: a fresh player, utterance ids just keep counting.
    channel.vc = vc2 = FakeVC()
    await rig.core.send(type="join")
    assert await rig.core.expect("joined", skip=()) == {"type": "joined", "channel": "客廳"}
    await rig.core.send(type="utterance_start", utterance=2)
    await rig.core.ws.send_bytes(her(2400))
    await rig.core.send(type="utterance_end", utterance=2)
    await until(lambda: vc2.plays == 1)
    await asyncio.to_thread(vc2.finish)
    assert (await rig.core.played_until(2, 2400))[-1] == 2400   # nothing of utterance 1 replayed or reported
    assert vc1.plays == 1
    await rig.core.lines.put(None)
    with pytest.raises(ConnectionError, match="closed the stream"):
        await asyncio.wait_for(dm_stream, 2)


async def test_link_dollcore_violation_report_then_reconnect(rig: Any, caplog: pytest.LogCaptureFixture) -> None:
    """dollcore's `error` (a violation it saw on our side) is logged with its reason, not echoed;
    its close follows and the link comes back."""
    await start(rig)
    rig.core.connected.clear()
    await rig.core.send(type="error", message="played: unknown utterance 9")
    await rig.core.ws.close(code=1002, message=b"played: unknown utterance 9")
    await asyncio.wait_for(rig.core.connected.wait(), 2)
    assert await rig.core.expect("hello", skip=()) == {"type": "hello", "version": 1}   # no `error` echoed back
    text = caplog.text
    assert "dollcore reports a protocol violation by the bridge: played: unknown utterance 9" in text
    assert "code 1002" in text and not rig.task.done()


async def test_link_reconnect_retries_with_backoff(rig: Any, caplog: pytest.LogCaptureFixture) -> None:
    await start(rig)
    rig.core.refuse = 2
    rig.core.connected.clear()
    began = time.monotonic()
    await rig.core.ws.close()
    await asyncio.wait_for(rig.core.connected.wait(), 3)
    waited = time.monotonic() - began
    assert await rig.core.expect("hello", skip=()) == {"type": "hello", "version": 1}
    failed = [r.getMessage() for r in caplog.records if "reconnecting to /api/voice failed" in r.getMessage()]
    assert len(failed) == 2 and all("409" in m for m in failed)
    assert "next try in 0.2 s" in failed[1]          # 0.05, 0.1, then capped at 0.2
    assert waited >= 0.05 + 0.1 + 0.2


async def test_link_startup_connect_failure_is_fatal(rig: Any) -> None:
    bad = CoreAPI(rig.api.base, "wrong", rig.session)
    link = VoiceLink(rig.link.cfg, OWNER, rig.link.client, bad.voice, decoder=FakeDecoder)
    with pytest.raises(ConnectionError, match="refused /api/voice: 401"):
        await asyncio.wait_for(link.run(), 2)


# --- against py-cord's real pieces (no network) -------------------------------------------------------------

LIBOPUS = VoiceConfig().opus_library


def test_sink_fits_pycords_event_router() -> None:
    from discord.voice.receive.router import SinkEventRouter

    sink, _, _, _ = make_sink([0.0])
    SinkEventRouter(sink, NS())                        # reads __sink_listeners__ and walk_children


@pytest.mark.skipif(not __import__("os").path.exists(LIBOPUS), reason=f"no libopus at {LIBOPUS}")
def test_real_opus_round_trip() -> None:
    from dollbridge.voice import load_opus

    load_opus(LIBOPUS)
    enc = opus.Encoder()
    s = Speaker(idle_hold_s=0.5, clock=lambda: 0.0)
    s.start(1)
    s.feed(pcm(tone(1000, 24000, 0.4)))
    s.end(1)
    packets = [enc.encode(f, enc.SAMPLES_PER_FRAME) for f in iter(s.read, SILENCE_FRAME)]
    assert len(packets) == 20
    got: list[bytes] = []
    sink = OwnerSink(OWNER, 0.2, lambda b, _: got.append(b), lambda: None)
    for i, p in enumerate(packets):
        sink.write(voice_packet(data=p, seq=i), NS(id=OWNER))
    out = samples(b"".join(got))
    assert len(out) == 20 * 320
    assert abs(rms(out[1600:]) / (10000 / np.sqrt(2)) - 1) < 0.1


def test_load_opus_failure_is_explicit() -> None:
    from dollbridge.voice import load_opus

    with pytest.raises(ValueError, match="cannot load libopus from /nonexistent/libopus.dylib"):
        load_opus("/nonexistent/libopus.dylib")


def test_example_config_documents_the_defaults() -> None:
    from pathlib import Path

    from dollbridge.__main__ import load_config

    example = Path(__file__).parent.parent / "discord.example.toml"
    assert load_config(example).voice == VoiceConfig()
