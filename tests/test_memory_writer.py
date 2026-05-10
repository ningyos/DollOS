"""Tests for memory_writer helpers."""

import re
from datetime import date
from pathlib import Path

import pytest

from dollos.memory_writer import append_transcript


class _FakeMemSearch:
    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


@pytest.mark.asyncio
async def test_append_transcript_writes_role_tagged_bullet(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path,
        memsearch=ms,
        role="user",
        text="hello",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert content.startswith("- ")
    assert "主人說：hello" in content


@pytest.mark.asyncio
async def test_append_transcript_appends_multiple(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="user", text="hi",
    )
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="doll", text="hello",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    content = expected.read_text()
    lines = [ln for ln in content.split("\n") if ln]
    assert len(lines) == 2
    assert "主人說：hi" in lines[0]
    assert "我說：hello" in lines[1]


@pytest.mark.asyncio
async def test_append_transcript_calls_index_file(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="user", text="x",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    assert ms.indexed == [expected]


@pytest.mark.asyncio
async def test_append_transcript_creates_parent_dir(tmp_path):
    ms = _FakeMemSearch()
    nested = tmp_path / "deep" / "transcripts"
    await append_transcript(
        transcripts_root=nested, memsearch=ms,
        role="user", text="x",
    )
    assert (nested / f"{date.today():%Y-%m-%d}.md").exists()


@pytest.mark.asyncio
async def test_append_transcript_uses_seconds_in_timestamp(tmp_path):
    ms = _FakeMemSearch()
    await append_transcript(
        transcripts_root=tmp_path, memsearch=ms,
        role="user", text="hi",
    )
    expected = tmp_path / f"{date.today():%Y-%m-%d}.md"
    content = expected.read_text()
    # Expect `- HH:MM:SS 主人說：hi` (3 colon-separated time groups).
    assert re.search(r"^- \d{2}:\d{2}:\d{2} 主人說：hi$", content.strip())
