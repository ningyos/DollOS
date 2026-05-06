"""Tests for Tool classes (Say, NoteMemory) and ToolCtx."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from dollos.ipc.messages import TextChunk
from dollos.tools import TOOLS, NoteMemory, Say, ToolCtx, WriteDiary


class _FakeMemSearch:
    """Fake MemSearch — captures index_file calls."""

    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


def _make_ctx(tmp_path: Path) -> tuple[ToolCtx, _FakeMemSearch, asyncio.Queue]:
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    return ctx, ms, sink


def test_say_schema_has_text_field():
    schema = Say.model_json_schema()
    assert "text" in schema["properties"]
    assert schema["properties"]["text"]["type"] == "string"


def test_note_memory_schema_has_text_field():
    schema = NoteMemory.model_json_schema()
    assert "text" in schema["properties"]


def test_tools_list_contains_both():
    assert Say in TOOLS
    assert NoteMemory in TOOLS


@pytest.mark.asyncio
async def test_say_run_pushes_text_chunk(tmp_path):
    ctx, _ms, sink = _make_ctx(tmp_path)
    say = Say(text="你好")
    await say.run(ctx)

    msg = sink.get_nowait()
    assert isinstance(msg, TextChunk)
    assert msg.text == "你好"


@pytest.mark.asyncio
async def test_note_memory_run_appends_bullet_to_daily_file(tmp_path):
    ctx, _ms, _sink = _make_ctx(tmp_path)
    note = NoteMemory(text="主人喜歡咖啡")
    await note.run(ctx)

    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected_path.exists()
    content = expected_path.read_text()
    assert content.endswith("- 主人喜歡咖啡\n")


@pytest.mark.asyncio
async def test_note_memory_run_calls_memsearch_index_file(tmp_path):
    ctx, ms, _sink = _make_ctx(tmp_path)
    note = NoteMemory(text="another fact")
    await note.run(ctx)

    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert ms.indexed == [expected_path]


@pytest.mark.asyncio
async def test_note_memory_run_appends_to_existing_file(tmp_path):
    ctx, _ms, _sink = _make_ctx(tmp_path)
    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    expected_path.parent.mkdir(parents=True)
    expected_path.write_text("# header\n\n- old fact\n")

    await NoteMemory(text="new fact").run(ctx)

    content = expected_path.read_text()
    assert "old fact" in content
    assert content.endswith("- new fact\n")


@pytest.mark.asyncio
async def test_write_diary_writes_markdown_section_and_indexes(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    diary = WriteDiary(content="今天我學會了 transcript 跟 diary。")
    await diary.run(ctx)

    expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "## 日記 (" in content
    assert "今天我學會了 transcript 跟 diary。" in content
    assert ms.indexed and Path(ms.indexed[-1]) == expected


def test_write_diary_schema_has_content_field():
    schema = WriteDiary.model_json_schema()
    assert "content" in schema["properties"]
    assert schema["properties"]["content"]["type"] == "string"


def test_write_diary_in_tools_list():
    from dollos.tools import TOOLS
    assert WriteDiary in TOOLS


@pytest.mark.asyncio
async def test_say_run_also_appends_to_transcript(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    transcripts_root = tmp_path / "transcripts"
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=transcripts_root,
    )
    await Say(text="hello").run(ctx)

    msg = sink.get_nowait()
    assert isinstance(msg, TextChunk) and msg.text == "hello"

    expected = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert "doll] hello" in content
    # Say writes to transcript; NoteMemory writes to shared.
    assert any(Path(p) == expected for p in ms.indexed)
