"""TTSObservingSink — a Queue subclass that enqueues TTS playback as a side
effect of `put_nowait(TextChunk(...))`.

The sink is otherwise a plain asyncio.Queue: items still flow to the IPC
pump unchanged. The voice session reference is fetched lazily via a
provider callable so sessions can be attached and detached without
re-wiring the sink at the call sites.

TextChunks are routed through `session.enqueue_speak(text)` so the
per-session serialized speak worker (Plan 2) plays them strictly FIFO,
avoiding audio overlap when multiple chunks arrive in rapid succession.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from dollos.ipc.messages import TextChunk

if TYPE_CHECKING:
    from dollos.voice.session import VoiceSession

logger = logging.getLogger(__name__)


class TTSObservingSink(asyncio.Queue):
    """asyncio.Queue subclass that fires `voice_session.enqueue_speak(text)` whenever
    a `TextChunk` is put into the queue. Other items pass through unchanged.
    """

    def __init__(
        self,
        *,
        voice_session_provider: Callable[[], "VoiceSession | None"],
        maxsize: int = 0,
    ) -> None:
        super().__init__(maxsize=maxsize)
        self._voice_session_provider = voice_session_provider

    def put_nowait(self, item: Any) -> None:
        super().put_nowait(item)
        if isinstance(item, TextChunk):
            session = self._voice_session_provider()
            # I6: gate on is_open so a task racing session.close() cannot
            # spawn a new speak-worker after the engine has been torn down.
            if session is not None and session.is_open:
                try:
                    asyncio.get_running_loop()
                    # Use enqueue_speak so the per-session worker serializes synth.
                    # Multiple TextChunks in a single turn play strictly FIFO.
                    asyncio.create_task(session.enqueue_speak(item.text))
                except RuntimeError:
                    logger.warning(
                        "TTSObservingSink: TextChunk put_nowait outside event "
                        "loop; TTS not scheduled (text=%r)", item.text,
                    )
                except Exception:
                    logger.exception("scheduling enqueue_speak failed")
