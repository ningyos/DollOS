"""SinkResolver — daemon-level current-sink lookup for Say streaming."""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class _SinkLike(Protocol):
    """Protocol for objects that accept put_nowait() calls."""
    def put_nowait(self, item) -> None: ...


class DummySink:
    """Drops all messages with a debug log. Used when no client connected."""
    def put_nowait(self, item) -> None:
        logger.debug("dummy sink dropped: %r", item)


class SinkResolver:
    """LIFO stack of registered sinks; resolves to most-recently-registered.
    Unregister removes from the stack."""

    def __init__(self) -> None:
        self._stack: list[_SinkLike] = []
        self._dummy = DummySink()

    def register(self, sink: _SinkLike) -> int:
        """Push a sink onto the stack. Returns a handle (current index)."""
        self._stack.append(sink)
        return len(self._stack) - 1  # handle

    def unregister(self, handle: int) -> None:
        """Remove the sink at this handle index, if still valid."""
        if 0 <= handle < len(self._stack):
            self._stack.pop(handle)

    def __call__(self) -> _SinkLike:
        """Resolve to the most-recently-registered sink, or DummySink."""
        if not self._stack:
            return self._dummy
        return self._stack[-1]
