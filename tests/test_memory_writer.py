"""Tests for memory_writer helpers."""

import re
from datetime import date
from pathlib import Path

import pytest

from dollos.memory_writer import append_transcript, append_action_log, is_action_log_line


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


class _FakeMem:
    def __init__(self): self.indexed = []
    async def index_file(self, p): self.indexed.append(p)


@pytest.mark.asyncio
async def test_append_action_log_writes_marked_line_and_indexes(tmp_path):
    mem = _FakeMem()
    await append_action_log(transcripts_root=tmp_path, memsearch=mem, phrase="我跑了指令 ls")
    f = tmp_path / f"{date.today():%Y-%m-%d}.md"
    text = f.read_text(encoding="utf-8")
    assert "▸ 我跑了指令 ls" in text
    assert re.match(r"^- \d{2}:\d{2}:\d{2} ▸ 我跑了指令 ls\n$", text)
    assert mem.indexed == [f]


def test_is_action_log_line_distinguishes_action_from_conversation():
    assert is_action_log_line("- 14:05:00 ▸ 我跑了指令 ls")
    assert is_action_log_line("- 23:00:01 ▸ Monitor mon-2 觸發:oom")
    assert not is_action_log_line("- 14:05:00 主人說：你在幹嘛")
    assert not is_action_log_line("- 14:05:00 我說：在看 log")
    assert not is_action_log_line("")
    assert not is_action_log_line("## 2026-07-07 日記")
