"""ChannelRegistry — daemon-side registry typing each I/O channel by locus
(internal=body organ / external=world) and kind (spec 2026-07-03 §3.1)."""
from __future__ import annotations

from dataclasses import dataclass

_LOCI = frozenset({"internal", "external"})


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    locus: str
    kind: str


class ChannelRegistry:
    """channel_id → ChannelInfo. Unknown channel_id resolves to locus
    'internal' (origin-less events are the existing internal path)."""

    def __init__(self) -> None:
        self._by_id: dict[str, ChannelInfo] = {}

    def register(self, channel_id: str, *, locus: str, kind: str) -> None:
        if locus not in _LOCI:
            raise ValueError(f"locus must be one of {_LOCI}, got {locus!r}")
        self._by_id[channel_id] = ChannelInfo(channel_id, locus, kind)

    def get(self, channel_id: str) -> ChannelInfo | None:
        return self._by_id.get(channel_id)

    def locus_of(self, channel_id: str | None) -> str:
        info = self._by_id.get(channel_id) if channel_id else None
        return info.locus if info is not None else "internal"

    def unregister(self, channel_id: str) -> None:
        self._by_id.pop(channel_id, None)
