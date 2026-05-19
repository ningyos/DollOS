"""ToolStreamParser — extract <tool_call> JSON blocks from a streaming text.

Legacy mode (voice_mode=False, default):
  - Text outside <tool_call>...</tool_call> markers is dropped (DEBUG log)
  - Returns list[dict] of parsed tool calls.

Voice mode (voice_mode=True):
  - Outside text is emitted as SpeakChunk events (for TTS).
  - Completed <tool_call> blocks are emitted as ToolCallReady events.
  - Returns list[SpeakChunk | ToolCallReady].

Used by EventDispatcher to route big-model output to tool dispatch + TTS.
"""

from __future__ import annotations

import json
import logging
from enum import Enum, auto

from dollos.stream_events import SpeakChunk, ToolCallReady

logger = logging.getLogger(__name__)

OPEN = "<tool_call>"
CLOSE = "</tool_call>"


class _State(Enum):
    OUTSIDE = auto()
    INSIDE = auto()


class ToolStreamParser:
    """State machine: accumulates stream chunks, yields parsed tool_call payloads.

    Caller pattern (legacy):
        for chunk in stream:
            for call in parser.feed(chunk):
                dispatch(call)
        for call in parser.flush():
            dispatch(call)

    Caller pattern (voice_mode=True):
        for chunk in stream:
            for event in parser.feed(chunk):
                if isinstance(event, SpeakChunk): ...
                elif isinstance(event, ToolCallReady): ...
        for event in parser.flush():
            ...
    """

    def __init__(self, voice_mode: bool = False) -> None:
        self._state = _State.OUTSIDE
        self._buf = ""           # rolling tail; may hold split markers
        self._inside_buf = ""    # accumulated JSON between markers
        self._voice_mode = voice_mode

    def feed(self, chunk: str):
        """Process a chunk.

        Legacy → list[dict]; voice mode → list[SpeakChunk | ToolCallReady].
        """
        self._buf += chunk
        if self._voice_mode:
            return list(self._feed_voice())
        return list(self._feed_legacy())

    def _feed_legacy(self):
        while True:
            if self._state is _State.OUTSIDE:
                idx = self._buf.find(OPEN)
                if idx < 0:
                    keep = len(OPEN) - 1
                    if len(self._buf) > keep:
                        dropped = self._buf[:-keep]
                        self._buf = self._buf[-keep:]
                        if dropped:
                            logger.debug("dropped naked text: %r", dropped)
                    break
                dropped = self._buf[:idx]
                if dropped:
                    logger.debug("dropped naked text: %r", dropped)
                self._buf = self._buf[idx + len(OPEN):]
                self._state = _State.INSIDE
                self._inside_buf = ""
            else:  # INSIDE
                idx = self._buf.find(CLOSE)
                if idx < 0:
                    keep = len(CLOSE) - 1
                    if len(self._buf) > keep:
                        self._inside_buf += self._buf[:-keep]
                        self._buf = self._buf[-keep:]
                    break
                self._inside_buf += self._buf[:idx]
                self._buf = self._buf[idx + len(CLOSE):]
                payload = self._inside_buf.strip()
                self._inside_buf = ""
                self._state = _State.OUTSIDE
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "malformed JSON in <tool_call>: %s; payload=%r",
                        e, payload,
                    )
                    continue
                if not isinstance(parsed, dict):
                    logger.warning(
                        "tool_call payload is not a JSON object: %r", parsed
                    )
                    continue
                yield parsed

    def _feed_voice(self):
        while True:
            if self._state is _State.OUTSIDE:
                i = self._buf.find(OPEN)
                if i == -1:
                    safe = max(0, len(self._buf) - (len(OPEN) - 1))
                    if safe > 0:
                        yield SpeakChunk(text=self._buf[:safe])
                        self._buf = self._buf[safe:]
                    return
                if i > 0:
                    yield SpeakChunk(text=self._buf[:i])
                self._buf = self._buf[i + len(OPEN):]
                self._state = _State.INSIDE
                self._inside_buf = ""
            else:  # INSIDE
                j = self._buf.find(CLOSE)
                if j == -1:
                    safe = max(0, len(self._buf) - (len(CLOSE) - 1))
                    if safe > 0:
                        self._inside_buf += self._buf[:safe]
                        self._buf = self._buf[safe:]
                    return
                self._inside_buf += self._buf[:j]
                self._buf = self._buf[j + len(CLOSE):]
                payload = self._inside_buf.strip()
                self._inside_buf = ""
                self._state = _State.OUTSIDE
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "malformed JSON in <tool_call>: %s; payload=%r",
                        e, payload,
                    )
                    continue
                if not isinstance(obj, dict) or "name" not in obj:
                    logger.warning(
                        "tool_call payload missing name or not object: %r", obj
                    )
                    continue
                yield ToolCallReady(
                    name=obj["name"],
                    arguments=obj.get("arguments") or {},
                )

    def flush(self):
        """Call at stream end.

        Legacy → []; voice mode → list of any final SpeakChunk for trailing text.
        """
        if self._voice_mode:
            return list(self._flush_voice())
        return list(self._flush_legacy())

    def _flush_legacy(self):
        if self._state is _State.INSIDE or self._inside_buf:
            logger.warning(
                "unclosed <tool_call> at stream end; dropped %r",
                self._inside_buf + self._buf,
            )
        elif self._buf:
            logger.debug("trailing naked text at stream end: %r", self._buf)
        self._buf = ""
        self._inside_buf = ""
        self._state = _State.OUTSIDE
        return []

    def _flush_voice(self):
        if self._state is _State.OUTSIDE:
            if self._buf:
                yield SpeakChunk(text=self._buf)
                self._buf = ""
        else:
            logger.warning(
                "voice flush with unclosed <tool_call>; dropped %r",
                self._inside_buf + self._buf,
            )
            self._inside_buf = ""
            self._buf = ""
            self._state = _State.OUTSIDE
