"""Tests for the embedder-free FTS5 + jieba memory backend (FtsMemory).

Covers: index_file + search round-trip, 繁體中文 recall, English recall,
idempotency, empty/no-match, dict shape, and H2 chunking.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.memory import FtsMemory


def _make(tmp_path: Path) -> FtsMemory:
    shared = tmp_path / "memory" / "shared"
    shared.mkdir(parents=True)
    return FtsMemory(paths=[str(shared)], db_path=tmp_path / "memory" / "fts.db")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / "memory" / "shared" / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


async def test_index_file_then_search_roundtrip(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "## 2026-06-26 10:00:00\n\nThe meeting about the rocket launch is at noon.\n",
        )
        await ms.index_file(p)
        hits = await ms.search("rocket launch", top_k=5)
        assert hits
        assert any("rocket launch" in h["content"] for h in hits)
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# 繁體中文 recall (load-bearing)
# ---------------------------------------------------------------------------


async def test_traditional_chinese_recall(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "## 2026-06-26 09:00:00\n\n主人最愛喝冰美式咖啡，不加糖。\n",
        )
        await ms.index_file(p)

        hits = await ms.search("美式", top_k=5)
        assert hits, "中文 single-term recall failed"
        assert "冰美式咖啡" in hits[0]["content"]

        hits2 = await ms.search("咖啡", top_k=5)
        assert hits2 and "咖啡" in hits2[0]["content"]
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# English recall still works (case-insensitive)
# ---------------------------------------------------------------------------


async def test_english_recall_case_insensitive(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-25.md",
            "## 2026-06-25 12:00:00\n\nThe user prefers iced Americano coffee without sugar.\n",
        )
        await ms.index_file(p)
        hits = await ms.search("AMERICANO", top_k=5)
        assert hits and "Americano" in hits[0]["content"]
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# mixed CJK + English in one note
# ---------------------------------------------------------------------------


async def test_mixed_cjk_english(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "## 2026-06-26 11:00:00\n\n主人在用 powdur 做語音測試。\n",
        )
        await ms.index_file(p)
        assert await ms.search("powdur", top_k=5)
        assert await ms.search("語音", top_k=5)
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# idempotency — re-index does not duplicate
# ---------------------------------------------------------------------------


async def test_index_file_idempotent(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "## 2026-06-26 10:00:00\n\nunique-token-xyzzy lives here.\n",
        )
        await ms.index_file(p)
        await ms.index_file(p)
        await ms.index_file(p)
        hits = await ms.search("xyzzy", top_k=10)
        assert len(hits) == 1, f"expected 1 hit, got {len(hits)} (duplication)"
    finally:
        ms.close()


async def test_index_file_reflects_edits(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(tmp_path, "2026-06-26.md", "## h\n\noldcontent-aaa\n")
        await ms.index_file(p)
        assert await ms.search("oldcontent-aaa", top_k=5)
        p.write_text("## h\n\nnewcontent-bbb\n", encoding="utf-8")
        await ms.index_file(p)
        assert not await ms.search("oldcontent-aaa", top_k=5)
        assert await ms.search("newcontent-bbb", top_k=5)
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# empty / no-match
# ---------------------------------------------------------------------------


async def test_no_match_returns_empty(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(tmp_path, "2026-06-26.md", "## h\n\nsomething ordinary.\n")
        await ms.index_file(p)
        assert await ms.search("nonexistent-term-qqqq", top_k=5) == []
    finally:
        ms.close()


async def test_empty_query_returns_empty(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(tmp_path, "2026-06-26.md", "## h\n\nbody text.\n")
        await ms.index_file(p)
        assert await ms.search("", top_k=5) == []
        assert await ms.search("   ", top_k=5) == []
    finally:
        ms.close()


async def test_search_on_empty_db(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        assert await ms.search("anything", top_k=5) == []
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# dict shape
# ---------------------------------------------------------------------------


async def test_hit_dict_shape(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "## 2026-06-26 10:00:00 [mood:calm tod:morning dow:Fri]\n\nshape-probe content.\n",
        )
        await ms.index_file(p)
        hits = await ms.search("shape-probe", top_k=5)
        assert hits
        h = hits[0]
        for key in ("content", "source", "heading", "chunk_hash", "score"):
            assert key in h, f"missing key {key!r}"
        # source is the file path and ends with the daily filename (date filter relies on this)
        assert h["source"].endswith("2026-06-26.md")
        # heading is the H2 text without leading "## " (context_tags.parse_heading parses it)
        assert h["heading"].startswith("2026-06-26 10:00:00")
        assert isinstance(h["score"], (int, float))
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# chunking by H2
# ---------------------------------------------------------------------------


async def test_chunking_by_h2(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        p = _write(
            tmp_path,
            "2026-06-26.md",
            "# 2026-06-26\n\n"
            "## 2026-06-26 09:00:00\n\nalpha section about dolphins.\n\n"
            "## 2026-06-26 18:00:00\n\nbeta section about penguins.\n",
        )
        await ms.index_file(p)
        # Each H2 is its own chunk: a query hitting one must not return the other.
        dolphin = await ms.search("dolphins", top_k=10)
        assert dolphin and len(dolphin) == 1
        assert "dolphins" in dolphin[0]["content"]
        assert "penguins" not in dolphin[0]["content"]
        # And the two headings are distinct chunks.
        headings = {h["heading"] for h in await ms.search("section", top_k=10)}
        assert "2026-06-26 09:00:00" in headings
        assert "2026-06-26 18:00:00" in headings
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# index() — full scan of configured paths
# ---------------------------------------------------------------------------


async def test_index_scans_all_files(tmp_path: Path):
    ms = _make(tmp_path)
    try:
        _write(tmp_path, "2026-06-24.md", "## h1\n\nfile-one apple.\n")
        _write(tmp_path, "2026-06-25.md", "## h2\n\nfile-two banana.\n")
        await ms.index()
        assert await ms.search("apple", top_k=5)
        assert await ms.search("banana", top_k=5)
    finally:
        ms.close()


async def test_index_is_full_rebuild(tmp_path: Path):
    """index() clears stale rows so a deleted file drops out."""
    ms = _make(tmp_path)
    try:
        p = _write(tmp_path, "2026-06-24.md", "## h\n\ntransient gamma.\n")
        await ms.index()
        assert await ms.search("gamma", top_k=5)
        p.unlink()
        await ms.index()
        assert await ms.search("gamma", top_k=5) == []
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# source_prefix scoping
# ---------------------------------------------------------------------------


async def test_source_prefix_filter(tmp_path: Path):
    base = tmp_path / "memory"
    shared = base / "shared"
    transcripts = base / "transcripts"
    shared.mkdir(parents=True)
    transcripts.mkdir(parents=True)
    ms = FtsMemory(paths=[str(shared), str(transcripts)], db_path=base / "fts.db")
    try:
        (shared / "2026-06-26.md").write_text("## h\n\nshared keyword-zeta.\n", encoding="utf-8")
        (transcripts / "2026-06-26.md").write_text(
            "## h\n\ntranscript keyword-zeta.\n", encoding="utf-8"
        )
        await ms.index()
        all_hits = await ms.search("keyword-zeta", top_k=10)
        assert len(all_hits) == 2
        scoped = await ms.search("keyword-zeta", top_k=10, source_prefix=str(shared))
        assert len(scoped) == 1
        assert scoped[0]["source"].endswith("shared/2026-06-26.md") or "shared" in scoped[0][
            "source"
        ]
    finally:
        ms.close()


async def test_source_prefix_trailing_sep_is_directory_boundary(tmp_path: Path):
    """Whole-branch verify: a source_prefix WITH a trailing separator is a
    directory boundary — ``.../shared/`` matches ``shared/…`` but NOT a
    sibling ``shared_backup/…`` that merely shares the string prefix.
    (A file-path prefix — no trailing sep — keeps its exact-prefix match; see
    the test above and tool_memory's tool_playbook.md usage.)"""
    base = tmp_path / "memory"
    shared = base / "shared"
    backup = base / "shared_backup"
    shared.mkdir(parents=True)
    backup.mkdir(parents=True)
    ms = FtsMemory(paths=[str(shared), str(backup)], db_path=base / "fts.db")
    try:
        (shared / "a.md").write_text("## h\n\nreal keyword-omega.\n", encoding="utf-8")
        (backup / "b.md").write_text("## h\n\nsibling keyword-omega.\n", encoding="utf-8")
        await ms.index()

        # Bare prefix (no trailing sep) is a string prefix → matches BOTH.
        bare = await ms.search("keyword-omega", top_k=10, source_prefix=str(shared))
        assert len(bare) == 2

        # Trailing separator → directory boundary → matches ONLY shared/.
        scoped = await ms.search("keyword-omega", top_k=10, source_prefix=str(shared) + "/")
        assert len(scoped) == 1
        assert "real" in scoped[0]["content"]
        assert "sibling" not in scoped[0]["content"]
    finally:
        ms.close()


@pytest.mark.parametrize("bad", ["AND", '"', "OR test", "(unbalanced"])
async def test_search_tolerates_fts_special_chars(tmp_path: Path, bad: str):
    """jieba tokens are quoted, so FTS5 operator-like queries don't raise."""
    ms = _make(tmp_path)
    try:
        p = _write(tmp_path, "2026-06-26.md", "## h\n\nplain body content here.\n")
        await ms.index_file(p)
        # Should not raise an sqlite FTS5 syntax error.
        await ms.search(bad, top_k=5)
    finally:
        ms.close()
