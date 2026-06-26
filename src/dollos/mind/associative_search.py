"""Context-associative recall.

Augments the existing semantic [Memory context] retrieval with a small
side-channel: memories that share the current mind-state context
(same mood / time-of-day / weekday) or that share the calendar date
(anniversary). Each axis contributes at most one hit; results are deduped
by chunk_hash and trimmed to top_k.

memsearch.search does NOT expose ``filter_expr`` to callers (only
``source_prefix``). So we post-filter: fetch a larger pool of semantic
hits, parse the ``heading`` of each hit with ``context_tags.parse_heading``,
and keep only those whose tags match the requested axis.

Graceful degradation: if an axis yields 0 matches, it's silently skipped
— no fallback, no fabrication, no error.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from dollos.mind.context_tags import (
    build_context_filter,
    dow_short,
    parse_heading,
    tod_bucket,
)

if TYPE_CHECKING:
    from dollos.memory import FtsMemory

    from dollos.mind.mind_state import MindState

logger = logging.getLogger(__name__)


# Per-axis fetch pool size. We fetch more than we keep so post-filter has
# something to choose from. 30 is a small cost (memsearch is fast) and
# gives a reasonable chance of finding a tag-matched hit.
_AXIS_POOL = 30


def _build_query(mind_state: "MindState") -> str:
    """Pull a short query string from recent UserSpoke perceptions."""
    parts: list[str] = []
    # Walk newest → oldest, keep up to 2 UserSpoke turns.
    for p in reversed(mind_state.recent_perceptions):
        if p.kind == "UserSpoke":
            txt = (p.data or {}).get("text", "")
            if txt:
                parts.append(txt[:200])
                if len(parts) >= 2:
                    break
    if not parts and mind_state.focus and mind_state.focus != "idle":
        parts.append(mind_state.focus)
    return " ".join(parts).strip()


def _match_axis(hit: dict, axis: str, value: str) -> bool:
    """Return True if hit's heading tags contain ``axis == value``.

    For axis=='date', value is "MM-DD" and we match against the heading's
    parsed date suffix (year-agnostic — anniversary).
    """
    parsed = parse_heading(hit.get("heading", "") or "")
    if axis == "date":
        d = parsed.get("date")
        if not d:
            return False
        return d[5:] == value  # "YYYY-MM-DD" → "MM-DD"
    return parsed.get("tags", {}).get(axis) == value


async def associative_search(
    memsearch: "FtsMemory",
    mind_state: "MindState",
    *,
    top_k: int = 3,
    now: datetime | None = None,
) -> list[dict]:
    """Per-axis associative recall. Returns up to ``top_k`` unique hits.

    Axes (each contributes at most 1 hit):
      - same mood (if current mood is set and slugifies non-empty)
      - same time-of-day bucket
      - same weekday
      - same calendar MM-DD (anniversary)

    Dedupe by chunk_hash. Skips axes that yield 0 matches.
    """
    if now is None:
        now = datetime.now()
    query = _build_query(mind_state)
    if not query:
        return []

    ctx_tags = build_context_filter(mind_state, now)

    # Axis list — only include axes we actually have a value for.
    axes: list[tuple[str, str]] = []
    if "mood" in ctx_tags:
        axes.append(("mood", ctx_tags["mood"]))
    axes.append(("tod", tod_bucket(now)))
    axes.append(("dow", dow_short(now)))
    axes.append(("date", now.strftime("%m-%d")))

    # One shared semantic pool — memsearch query is identical across axes,
    # only the post-filter differs. Saves N round-trips.
    try:
        pool = await memsearch.search(query, top_k=_AXIS_POOL)
    except Exception:
        logger.exception("associative_search: memsearch query failed")
        return []

    seen_hashes: set[str] = set()
    results: list[dict] = []

    for axis, value in axes:
        for hit in pool:
            ch = hit.get("chunk_hash") or hit.get("content", "")[:80]
            if ch in seen_hashes:
                continue
            if not _match_axis(hit, axis, value):
                continue
            # Annotate which axis matched (for prompt rendering).
            annotated = dict(hit)
            annotated["_axis"] = axis
            annotated["_axis_value"] = value
            results.append(annotated)
            seen_hashes.add(ch)
            break  # one hit per axis

    return results[:top_k]
