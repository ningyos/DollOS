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
    """Dict-based sink registry keyed by monotonic handle. Resolves by origin:
    an external sink only when the turn's origin channel_id matches it; else
    the most-recently-registered INTERNAL sink (a connected external bridge
    never steals origin-less internal output — spec 2026-07-03 §3.1 I2).

    Handles are monotonically increasing integers — they remain stable
    regardless of removal order, so unregister(A) before unregister(B) never
    corrupts B's handle.
    """

    def __init__(self) -> None:
        self._sinks: dict[int, _SinkLike] = {}
        self._meta: dict[int, tuple[str, str | None]] = {}   # handle → (locus, channel_id)
        self._counter: int = 0
        self._dummy = DummySink()

    def register(self, sink: _SinkLike, *, locus: str = "internal",
                 channel_id: str | None = None) -> int:
        """Register a sink with its locus/channel. Bare register(sink) keeps
        the legacy internal-sink behavior (back-compat)."""
        handle = self._counter
        self._counter += 1
        self._sinks[handle] = sink
        self._meta[handle] = (locus, channel_id)
        return handle

    def unregister(self, handle: int) -> None:
        """Remove the sink with this handle. No-op if already removed."""
        self._sinks.pop(handle, None)
        self._meta.pop(handle, None)

    def __call__(self, origin: str | None = None) -> _SinkLike:
        """Resolve the sink for this turn's origin channel. External sink only
        when origin matches its channel_id; otherwise the most-recent INTERNAL
        sink (R1-arch I2: a connected external bridge must not steal internal
        output). DummySink when nothing suitable."""
        if origin is not None:
            for h in sorted(self._sinks, reverse=True):
                loc, cid = self._meta[h]
                if loc == "external" and cid == origin:
                    return self._sinks[h]
        # origin-less, or no external match → most-recent internal
        internal = [h for h in self._sinks if self._meta[h][0] == "internal"]
        if internal:
            return self._sinks[max(internal)]
        return self._dummy
