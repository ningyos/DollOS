"""TTSObservingSink — a Queue subclass that triggers TTS as a side
effect of `put_nowait(TextChunk(...))`.

The sink is otherwise a plain asyncio.Queue: items still flow to the
IPC pump unchanged. The voice session reference is fetched lazily via
a provider callable so sessions can be attached and detached without
re-wiring the sink at the call sites.
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
    """asyncio.Queue subclass that fires `voice_session.speak(text)` whenever
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
            if session is not None:
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(session.speak(item.text))
                except RuntimeError:
                    logger.warning(
                        "TTSObservingSink: TextChunk put_nowait outside event "
                        "loop; TTS not scheduled (text=%r)", item.text,
                    )
                except Exception:
                    logger.exception("scheduling speak() failed")
