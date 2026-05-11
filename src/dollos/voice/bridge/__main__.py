"""Local audio bridge CLI.

Usage:
    python -m dollos.voice.bridge --daemon ws://localhost:9876

Captures mic, runs silero VAD, streams to daemon over WebRTC, plays
daemon's TTS output. Press Ctrl-C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import websockets

from dollos.voice.bridge.controller import BridgeController
from dollos.voice.bridge.mic import MicrophoneTrack
from dollos.voice.bridge.signaling import BridgeSignaling
from dollos.voice.bridge.speaker import SpeakerPlayer
from dollos.voice.bridge.vad import SileroVAD


logger = logging.getLogger("dollos.voice.bridge")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.voice.bridge",
        description="Local audio bridge — mic + speaker WebRTC client for DollOS",
    )
    p.add_argument(
        "--daemon", type=str, default="ws://127.0.0.1:9876",
        help="Daemon WS URL (default ws://127.0.0.1:9876)",
    )
    p.add_argument(
        "--data-root", type=Path, default=Path("data"),
        help="data root for VAD model cache (default ./data)",
    )
    p.add_argument(
        "--mic-rate", type=int, default=16000,
        help="Microphone sample rate (default 16000 — matches VAD + ASR)",
    )
    p.add_argument(
        "--speaker-rate", type=int, default=48000,
        help="Speaker output sample rate (default 48000 — luxtts native)",
    )
    p.add_argument(
        "--mic-device", type=int, default=None,
        help="Optional sounddevice input device index",
    )
    p.add_argument("--verbose", action="store_true")
    return p


async def run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    logger.info("connecting to daemon: %s", args.daemon)
    async with websockets.connect(args.daemon) as ws:
        vad = SileroVAD(data_root=args.data_root)
        mic = MicrophoneTrack(
            sample_rate=args.mic_rate, device=args.mic_device,
        )
        speaker = SpeakerPlayer(sample_rate=args.speaker_rate)
        signaling = BridgeSignaling(ws=ws)
        controller = BridgeController(
            signaling=signaling, vad=vad, sample_rate=args.mic_rate,
        )

        async def _on_remote_track(track) -> None:
            await speaker.consume_track(track)

        await signaling.connect(
            local_audio_track=mic, on_remote_track=_on_remote_track,
        )
        logger.info("bridge connected — speak any time. Ctrl-C to quit.")

        mic_loop = asyncio.create_task(
            controller.run_mic_loop(mic), name="bridge-mic-loop",
        )

        stop_event = asyncio.Event()

        def _sigint_handler() -> None:
            logger.info("SIGINT — shutting down")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            loop.add_signal_handler(getattr(signal, sig_name), _sigint_handler)

        try:
            await stop_event.wait()
        finally:
            mic_loop.cancel()
            try:
                await mic_loop
            except (asyncio.CancelledError, Exception):
                pass
            mic.stop()
            speaker.stop()
            await signaling.close()
            vad.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
