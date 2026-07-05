"""P1e Task 3 (S2): NoteMemory/WriteDiary write to a directory chosen by
``ctx.origin_tier``, so origin is encoded by PATH (not a schema column) — a
later task (Task 4) can scope retrieval by ``source_prefix``.

- internal        -> memory_root/shared/{date}.md            (unchanged)
- external_public  -> memory_root/external_public/{date}.md
- external_dm      -> memory_root/external_dm/{date}.md

Also asserts memsearch.index_file was called with that exact path, so
Task 4's source_prefix scoping has something to filter on.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests._dispatcher_helpers import _make_mind_ctx
from dollos.tools import NoteMemory, WriteDiary

_CASES = [
    ("internal", "shared"),
    ("external_public", "external_public"),
    ("external_dm", "external_dm"),
]


@pytest.mark.parametrize("origin_tier,subdir", _CASES)
@pytest.mark.asyncio
async def test_note_memory_routes_by_origin_tier(tmp_path, origin_tier, subdir):
    ctx = _make_mind_ctx(tmp_path)
    ctx.origin_tier = origin_tier

    await NoteMemory(text="a fact").run(ctx)

    expected_path = tmp_path / subdir / f"{date.today():%Y-%m-%d}.md"
    assert expected_path.exists()
    assert "a fact" in expected_path.read_text()
    assert ctx.memsearch.indexed == [expected_path]


@pytest.mark.parametrize("origin_tier,subdir", _CASES)
@pytest.mark.asyncio
async def test_write_diary_routes_by_origin_tier(tmp_path, origin_tier, subdir):
    ctx = _make_mind_ctx(tmp_path)
    ctx.origin_tier = origin_tier

    await WriteDiary(content="today's diary").run(ctx)

    expected_path = tmp_path / subdir / f"{date.today():%Y-%m-%d}.md"
    assert expected_path.exists()
    assert "today's diary" in expected_path.read_text()
    assert ctx.memsearch.indexed and Path(ctx.memsearch.indexed[-1]) == expected_path
