"""SinkResolver — daemon-level current-sink lookup for streaming Doll output."""
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
    """Dict-based sink registry keyed by monotonic handle; resolves to
    most-recently-registered sink.

    Handles are monotonically increasing integers — they remain stable
    regardless of removal order, so unregister(A) before unregister(B) never
    corrupts B's handle.
    """

    def __init__(self) -> None:
        self._sinks: dict[int, _SinkLike] = {}
        self._counter: int = 0
        self._dummy = DummySink()

    def register(self, sink: _SinkLike) -> int:
        """Register a sink. Returns a stable handle (monotonic counter)."""
        handle = self._counter
        self._counter += 1
        self._sinks[handle] = sink
        return handle

    def unregister(self, handle: int) -> None:
        """Remove the sink with this handle. No-op if already removed."""
        self._sinks.pop(handle, None)

    def __call__(self) -> _SinkLike:
        """Resolve to the most-recently-registered sink, or DummySink."""
        if not self._sinks:
            return self._dummy
        return self._sinks[max(self._sinks)]
