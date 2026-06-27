"""Tests for context-associative recall."""
from __future__ import annotations

from datetime import datetime

import pytest

from dollos.mind.associative_search import associative_search
from dollos.mind.context_tags import (
    build_context_filter,
    build_heading,
    dow_short,
    format_tag_block,
    parse_heading,
    slugify_focus,
    tod_bucket,
)
from dollos.mind.mind_state import MindState, Mood, Perception

# ---------------------------------------------------------------------------
# context_tags — round-trip
# ---------------------------------------------------------------------------


def test_tod_buckets():
    assert tod_bucket(datetime(2026, 5, 18, 7, 0)) == "morning"
    assert tod_bucket(datetime(2026, 5, 18, 13, 0)) == "afternoon"
    assert tod_bucket(datetime(2026, 5, 18, 19, 0)) == "evening"
    assert tod_bucket(datetime(2026, 5, 18, 23, 0)) == "late_night"
    assert tod_bucket(datetime(2026, 5, 18, 3, 0)) == "late_night"


def test_dow_short():
    # 2026-05-18 is a Monday
    assert dow_short(datetime(2026, 5, 18)) == "Mon"
    assert dow_short(datetime(2026, 5, 24)) == "Sun"


def test_slugify_focus():
    assert slugify_focus("Powdur Voice Tuning") == "powdur-voice-tuning"
    assert slugify_focus("  焦慮  X y ") == "x-y"  # CJK stripped
    assert slugify_focus("") is None
    assert slugify_focus(None) is None
    assert slugify_focus("idle!@#") == "idle"


def test_build_context_filter_includes_all_axes():
    state = MindState(mood=Mood(emotion="anxious"), focus="powdur voice tuning")
    now = datetime(2026, 5, 18, 19, 30)
    tags = build_context_filter(state, now)
    assert tags["mood"] == "anxious"
    assert tags["focus"] == "powdur-voice-tuning"
    assert tags["tod"] == "evening"
    assert tags["dow"] == "Mon"


def test_build_context_filter_omits_missing():
    # No mood emotion → omitted; focus 'idle' → omitted (sentinel).
    state = MindState(mood=Mood(emotion=""), focus="idle")
    tags = build_context_filter(state, datetime(2026, 5, 18, 13, 0))
    assert "mood" not in tags
    assert "focus" not in tags
    assert tags["tod"] == "afternoon"
    assert tags["dow"] == "Mon"


def test_heading_round_trip():
    state = MindState(mood=Mood(emotion="happy"), focus="testing")
    now = datetime(2026, 5, 18, 22, 34, 12)
    heading = build_heading(state, now)
    assert heading.startswith("2026-05-18 22:34:12")
    assert "mood:happy" in heading
    assert "focus:testing" in heading
    assert "tod:late_night" in heading
    assert "dow:Mon" in heading

    parsed = parse_heading(heading)
    assert parsed["date"] == "2026-05-18"
    assert parsed["time"] == "22:34:12"
    assert parsed["tags"]["mood"] == "happy"
    assert parsed["tags"]["focus"] == "testing"
    assert parsed["tags"]["tod"] == "late_night"
    assert parsed["tags"]["dow"] == "Mon"


def test_parse_legacy_heading_returns_empty_tags():
    # Old-format heading (no [...] block)
    parsed = parse_heading("2026-05-18 12:00:00")
    assert parsed["date"] == "2026-05-18"
    assert parsed["tags"] == {}

    # Completely unrecognized
    parsed = parse_heading("日記 (12:34:56)")
    assert parsed["date"] is None
    assert parsed["tags"] == {}

    # Empty
    parsed = parse_heading("")
    assert parsed["date"] is None


def test_format_tag_block_empty():
    assert format_tag_block({}) == ""
    assert format_tag_block({"mood": "ok"}) == "[mood:ok]"


# ---------------------------------------------------------------------------
# I2 regression — Chinese mood must NOT be stripped
# ---------------------------------------------------------------------------


def test_build_context_filter_chinese_mood_non_empty():
    """I2: Chinese emotion string must produce a non-empty mood tag."""
    state = MindState(mood=Mood(emotion="平靜"))
    tags = build_context_filter(state, datetime(2026, 5, 18, 19, 30))
    assert "mood" in tags, "mood axis must be present for Chinese emotion"
    assert tags["mood"] == "平靜"


def test_build_heading_chinese_mood_present_in_heading():
    """I2: build_heading must include mood:平靜 tag for Chinese emotion."""
    state = MindState(mood=Mood(emotion="平靜"))
    now = datetime(2026, 5, 18, 22, 34, 12)
    heading = build_heading(state, now)
    assert "mood:平靜" in heading, f"Expected mood:平靜 in heading, got: {heading!r}"


def test_build_context_filter_chinese_mood_with_spaces():
    """I2: spaces in Chinese emotion are replaced with '_', not stripped."""
    state = MindState(mood=Mood(emotion="有點 焦慮"))
    tags = build_context_filter(state, datetime(2026, 5, 18, 8, 0))
    assert "mood" in tags
    assert tags["mood"] == "有點_焦慮"


def test_build_context_filter_mood_bracket_sanitized():
    """I2: ']' in mood value is removed to avoid breaking the heading format."""
    state = MindState(mood=Mood(emotion="ok]done"))
    tags = build_context_filter(state, datetime(2026, 5, 18, 8, 0))
    assert "mood" in tags
    assert tags["mood"] == "okdone"


def test_build_heading_chinese_mood_round_trips():
    """I2: parse_heading recovers the Chinese mood tag written by build_heading."""
    state = MindState(mood=Mood(emotion="焦慮"))
    now = datetime(2026, 5, 18, 22, 34, 12)
    heading = build_heading(state, now)
    parsed = parse_heading(heading)
    assert parsed["tags"].get("mood") == "焦慮", (
        f"Expected tags['mood']=='焦慮', got {parsed['tags']!r}"
    )


# ---------------------------------------------------------------------------
# associative_search — stubbed memsearch
# ---------------------------------------------------------------------------


class _StubMemSearch:
    """Returns a fixed pool of hits regardless of query."""

    def __init__(self, hits: list[dict]):
        self._hits = hits
        self.queries: list[str] = []

    async def search(self, query, *, top_k=10, **_kw):
        self.queries.append(query)
        return list(self._hits[:top_k])


def _hit(content: str, heading: str, chunk_hash: str | None = None) -> dict:
    return {
        "content": content,
        "heading": heading,
        "chunk_hash": chunk_hash or content[:30],
        "source": "/x.md",
    }


@pytest.mark.asyncio
async def test_associative_search_matches_each_axis():
    # Build a pool with one hit per axis
    pool = [
        _hit("anxious memory", "2026-04-10 19:00:00 [mood:anxious tod:evening dow:Fri]"),
        _hit("evening memory", "2026-04-11 19:30:00 [mood:calm tod:evening dow:Sat]"),
        _hit("monday memory", "2026-04-13 09:00:00 [mood:calm tod:morning dow:Mon]"),
        _hit("anniversary mem", "2025-05-18 11:00:00 [mood:calm tod:morning dow:Sun]"),
        _hit("noise A", "2026-04-12 09:00:00 [mood:bored tod:morning dow:Sun]"),
    ]
    ms = _StubMemSearch(pool)
    state = MindState(mood=Mood(emotion="anxious"), focus="something")
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=0.0, data={"text": "tell me about it"})
    )
    now = datetime(2026, 5, 18, 19, 30)  # Mon, evening, 05-18

    hits = await associative_search(ms, state, top_k=4, now=now)
    # mood:anxious, tod:evening, dow:Mon, date 05-18 all match
    contents = {h["content"] for h in hits}
    axes = {h["_axis"] for h in hits}
    assert "mood" in axes
    assert "tod" in axes
    assert "dow" in axes
    assert "date" in axes
    assert "anxious memory" in contents
    assert "anniversary mem" in contents


@pytest.mark.asyncio
async def test_associative_search_dedupes_by_chunk_hash():
    # Same hit matches multiple axes — should appear once.
    pool = [
        _hit(
            "all-match",
            "2026-05-18 19:00:00 [mood:anxious tod:evening dow:Mon]",
            chunk_hash="X",
        ),
    ]
    ms = _StubMemSearch(pool)
    state = MindState(mood=Mood(emotion="anxious"))
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=0.0, data={"text": "hi"})
    )
    now = datetime(2026, 5, 18, 19, 30)
    hits = await associative_search(ms, state, top_k=4, now=now)
    assert len(hits) == 1
    assert hits[0]["_axis"] in {"mood", "tod", "dow", "date"}


@pytest.mark.asyncio
async def test_associative_search_skips_unmatched_axis():
    # No mood-match in pool — axis silently skipped.
    pool = [
        _hit("evening only", "2026-04-11 19:30:00 [mood:bored tod:evening dow:Sat]"),
    ]
    ms = _StubMemSearch(pool)
    state = MindState(mood=Mood(emotion="anxious"))
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=0.0, data={"text": "hi"})
    )
    now = datetime(2026, 5, 18, 19, 30)
    hits = await associative_search(ms, state, top_k=4, now=now)
    # tod:evening matches; nothing else does
    assert len(hits) == 1
    assert hits[0]["_axis"] == "tod"


@pytest.mark.asyncio
async def test_associative_search_no_query_returns_empty():
    state = MindState()  # no perceptions, no focus
    ms = _StubMemSearch([_hit("x", "2026-05-18 19:00:00 [tod:evening dow:Mon]")])
    hits = await associative_search(ms, state)
    assert hits == []
    assert ms.queries == []  # no query → no search


@pytest.mark.asyncio
async def test_associative_search_legacy_headings_yield_no_match():
    # Old-format entries (no tag block) must NEVER match — backwards compat.
    pool = [
        _hit("legacy bullet", ""),
        _hit("legacy diary", "日記 (12:34:56)"),
    ]
    ms = _StubMemSearch(pool)
    state = MindState(mood=Mood(emotion="anxious"))
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=0.0, data={"text": "hi"})
    )
    now = datetime(2026, 5, 18, 19, 30)
    hits = await associative_search(ms, state, top_k=4, now=now)
    assert hits == []


# ---------------------------------------------------------------------------
# End-to-end with the real FtsMemory backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_associative_search_e2e_real_fts_memory(tmp_path):
    from dollos.memory import FtsMemory

    # Write a few markdown files with tagged headings.
    md = tmp_path / "shared" / "2026-05-18.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "## 2026-05-18 09:00:00 [mood:calm tod:morning dow:Mon]\n\n"
        "Morning monday memory about coffee.\n\n"
        "## 2026-05-18 19:00:00 [mood:anxious tod:evening dow:Mon]\n\n"
        "Anxious evening monday note about a powdur voice test.\n\n"
        "## 2025-05-18 14:00:00 [mood:calm tod:afternoon dow:Sun]\n\n"
        "Anniversary memory from a year ago today — feeling reflective.\n"
    )

    ms = FtsMemory(
        paths=[str(tmp_path / "shared")],
        db_path=tmp_path / "fts.db",
    )
    await ms.index()

    state = MindState(mood=Mood(emotion="anxious"))
    state.recent_perceptions.append(
        Perception(kind="UserSpoke", t=0.0, data={"text": "powdur voice test"})
    )
    now = datetime(2026, 5, 18, 19, 30)  # Mon, evening, 05-18

    hits = await associative_search(ms, state, top_k=4, now=now)
    axes = {h["_axis"] for h in hits}
    contents = " ".join(h["content"] for h in hits)
    # We expect at least anxious-mood + evening-tod + Mon-dow + 05-18 date
    assert axes  # non-empty
    assert "Anxious evening monday" in contents or "Anniversary" in contents
    ms.close()
