"""P1e Task 4 (S3): ``FtsMemory.search(..., exclude_prefixes=...)`` — a real
SQL-level ``NOT LIKE`` exclusion (not a post-hoc Python filter), so an
external_public (stranger) turn's retrieval can never surface the owner's
private-tier memory.

Private tier = ``{shared/, external_dm/}`` (P1e Task 3, S2). Only
``external_public/`` is safe to serve to a stranger.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dollos.memory import FtsMemory


def _make(tmp_path: Path, *extra_dirs: str) -> tuple[FtsMemory, Path]:
    base = tmp_path / "memory"
    shared = base / "shared"
    public = base / "external_public"
    shared.mkdir(parents=True)
    public.mkdir(parents=True)
    paths = [str(shared), str(public)]
    for d in extra_dirs:
        p = base / d
        p.mkdir(parents=True)
        paths.append(str(p))
    ms = FtsMemory(paths=paths, db_path=base / "fts.db")
    return ms, base


async def test_exclude_prefixes_filters_out_private_tier(tmp_path: Path):
    ms, base = _make(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate marker unique-alpha-999.\n", encoding="utf-8"
        )
        (base / "external_public" / "y.md").write_text(
            "## h\n\npublic marker unique-alpha-999.\n", encoding="utf-8"
        )
        await ms.index()

        # Control: without exclude, both the private and public note match.
        all_hits = await ms.search("unique-alpha-999", top_k=5)
        assert len(all_hits) == 2

        scoped = await ms.search(
            "unique-alpha-999", top_k=5, exclude_prefixes=[base / "shared"]
        )
        assert len(scoped) == 1
        assert "public marker" in scoped[0]["content"]
        assert "private marker" not in scoped[0]["content"]
    finally:
        ms.close()


async def test_exclude_prefixes_has_teeth_private_never_returned(tmp_path: Path):
    """Invert the assertion to prove the filter has real teeth: if
    ``exclude_prefixes`` were a no-op, the private note would still be
    returned and this ``assert`` would NOT raise — so wrapping it in
    ``pytest.raises(AssertionError)`` itself must succeed."""
    ms, base = _make(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate marker unique-beta-111.\n", encoding="utf-8"
        )
        await ms.index()

        scoped = await ms.search(
            "unique-beta-111", top_k=5, exclude_prefixes=[base / "shared"]
        )
        with pytest.raises(AssertionError):
            assert any("private marker" in h["content"] for h in scoped)
    finally:
        ms.close()


async def test_exclude_prefixes_multiple_prefixes_combined(tmp_path: Path):
    """The real private-tier set is TWO dirs (shared/ + external_dm/) — both
    excluded together, external_public/ still retrievable."""
    ms, base = _make(tmp_path, "external_dm")
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate-shared unique-gamma-222.\n", encoding="utf-8"
        )
        (base / "external_dm" / "y.md").write_text(
            "## h\n\nprivate-dm unique-gamma-222.\n", encoding="utf-8"
        )
        (base / "external_public" / "z.md").write_text(
            "## h\n\npublic-note unique-gamma-222.\n", encoding="utf-8"
        )
        await ms.index()

        scoped = await ms.search(
            "unique-gamma-222",
            top_k=10,
            exclude_prefixes=[base / "shared", base / "external_dm"],
        )
        assert len(scoped) == 1
        assert "public-note" in scoped[0]["content"]
    finally:
        ms.close()


async def test_exclude_prefix_is_directory_boundary_not_string_prefix(tmp_path: Path):
    """Excluding ``shared`` must not also swallow a sibling ``shared_backup``
    — the exclude clause appends a ``/`` boundary so it's a directory prefix,
    not a bare string prefix."""
    ms, base = _make(tmp_path, "shared_backup")
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate marker unique-eps-777.\n", encoding="utf-8"
        )
        (base / "shared_backup" / "y.md").write_text(
            "## h\n\nsibling marker unique-eps-777.\n", encoding="utf-8"
        )
        await ms.index()

        scoped = await ms.search(
            "unique-eps-777", top_k=5, exclude_prefixes=[base / "shared"]
        )
        # shared/ excluded, shared_backup/ NOT excluded (distinct directory).
        assert len(scoped) == 1
        assert "sibling marker" in scoped[0]["content"]
    finally:
        ms.close()


async def test_exclude_prefixes_none_is_unchanged_default(tmp_path: Path):
    """Regression guard: omitting exclude_prefixes (or passing None) behaves
    exactly like before this feature — full recall, no filtering."""
    ms, base = _make(tmp_path)
    try:
        (base / "shared" / "x.md").write_text(
            "## h\n\nprivate marker unique-delta-333.\n", encoding="utf-8"
        )
        (base / "external_public" / "y.md").write_text(
            "## h\n\npublic marker unique-delta-333.\n", encoding="utf-8"
        )
        await ms.index()

        default_hits = await ms.search("unique-delta-333", top_k=5)
        explicit_none_hits = await ms.search(
            "unique-delta-333", top_k=5, exclude_prefixes=None
        )
        assert len(default_hits) == 2
        assert len(explicit_none_hits) == 2
    finally:
        ms.close()
