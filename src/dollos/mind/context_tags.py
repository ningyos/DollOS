"""Context tags for context-associative recall.

Encode current mind-state context (mood / focus / time-of-day / weekday)
into the markdown H2 heading of each memory entry. memsearch chunks by
heading, so these tags travel with the chunk and can be parsed back
client-side to filter associative recall.

Heading format:

    ## 2026-05-18 22:34:12 [mood:anxious focus:powdur-voice-tuning tod:evening dow:Mon]

The leading timestamp is always present; tag pairs inside ``[...]`` are
optional and space-separated ``key:value``. Backward compat: legacy
headings without the ``[...]`` block parse to ``tags={}`` and are
naturally excluded from any tag-filtered search.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.mind.mind_state import MindState


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------


def tod_bucket(now: datetime) -> str:
    """Return time-of-day bucket label."""
    h = now.hour
    if 5 <= h <= 11:
        return "morning"
    if 12 <= h <= 16:
        return "afternoon"
    if 17 <= h <= 21:
        return "evening"
    return "late_night"  # 22-04


_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def dow_short(now: datetime) -> str:
    return _DOW[now.weekday()]


_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")


def slugify_focus(focus: str | None) -> str | None:
    """Lowercase, whitespace→'-', strip non-[a-z0-9-]. Empty → None."""
    if not focus:
        return None
    s = focus.lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_STRIP.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or None


# ---------------------------------------------------------------------------
# Build (writer side)
# ---------------------------------------------------------------------------


def build_context_filter(mind_state: "MindState", now: datetime | None = None) -> dict[str, str]:
    """Snapshot the current context as a {tag_key: value} dict.

    Used by both:
      - heading writers (encode every key that's set)
      - associative_search (build axis filters from current context)

    Missing axes are simply omitted — never faked.
    """
    if now is None:
        now = datetime.now()
    tags: dict[str, str] = {}

    mood = getattr(mind_state.mood, "emotion", None) if mind_state.mood else None
    mood_slug = slugify_focus(mood)
    if mood_slug:
        tags["mood"] = mood_slug

    focus_slug = slugify_focus(mind_state.focus) if mind_state.focus and mind_state.focus != "idle" else None
    if focus_slug:
        tags["focus"] = focus_slug

    tags["tod"] = tod_bucket(now)
    tags["dow"] = dow_short(now)
    return tags


def format_tag_block(tags: dict[str, str]) -> str:
    """Render tags dict to the ``[k:v k:v]`` suffix. Empty → ''."""
    if not tags:
        return ""
    parts = [f"{k}:{v}" for k, v in tags.items()]
    return "[" + " ".join(parts) + "]"


def build_heading(mind_state: "MindState", now: datetime | None = None) -> str:
    """Build the H2 heading text (without the leading ``## ``).

    Format: ``YYYY-MM-DD HH:MM:SS [k:v k:v ...]`` (tag block omitted if empty).
    """
    if now is None:
        now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    tags = build_context_filter(mind_state, now)
    block = format_tag_block(tags)
    return f"{ts} {block}" if block else ts


# ---------------------------------------------------------------------------
# Parse (reader side)
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})(?:\s+\[(?P<body>[^\]]*)\])?"
)


def parse_heading(heading: str) -> dict:
    """Parse a heading string back into a dict.

    Returns ``{"date": "YYYY-MM-DD", "time": "HH:MM:SS", "tags": {...}}``
    on match, or ``{"date": None, "time": None, "tags": {}}`` for legacy /
    unrecognized headings.
    """
    if not heading:
        return {"date": None, "time": None, "tags": {}}
    m = _HEADING_RE.match(heading.strip())
    if not m:
        return {"date": None, "time": None, "tags": {}}
    tags: dict[str, str] = {}
    body = m.group("body")
    if body:
        for tok in body.split():
            if ":" in tok:
                k, _, v = tok.partition(":")
                if k and v:
                    tags[k] = v
    return {"date": m.group("date"), "time": m.group("time"), "tags": tags}
