"""ToolStreamParser — extract <tool_call> JSON blocks from a streaming text.

Stateless drop-everything-outside policy:
  - Text outside <tool_call>...</tool_call> markers is dropped (DEBUG log)
  - Inside markers: accumulate and json.loads on </tool_call>
  - Malformed JSON: WARNING + skip + reset to OUTSIDE
  - Unclosed at flush(): WARNING + drop

Used by EventDispatcher to route big-model output to tool dispatch.
"""

from __future__ import annotations

import json
import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)

OPEN = "<tool_call>"
CLOSE = "</tool_call>"


class _State(Enum):
    OUTSIDE = auto()
    INSIDE = auto()


class ToolStreamParser:
    """State machine: accumulates stream chunks, yields parsed tool_call dicts.

    Caller pattern:
        for chunk in stream:
            for call in parser.feed(chunk):
                dispatch(call)
        for call in parser.flush():
            dispatch(call)
    """

    def __init__(self) -> None:
        self._state = _State.OUTSIDE
        self._buf = ""           # rolling tail; may hold split markers
        self._inside_buf = ""    # accumulated JSON between markers

    def feed(self, chunk: str) -> list[dict]:
        """Process a chunk; return zero or more parsed tool_call dicts."""
        self._buf += chunk
        out: list[dict] = []
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
                out.append(parsed)
        return out

    def flush(self) -> list[dict]:
        """Call at stream end. Logs unclosed tool_call if any; returns [].

        Always returns an empty list — flush is for cleanup logging.
        """
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
