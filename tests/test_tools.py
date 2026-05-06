"""Tests for Tool classes (Say, NoteMemory) and ToolCtx."""

import asyncio
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.ipc.messages import TextChunk
from dollos.tools import (
    SHELL_DEFAULT_TIMEOUT_S,
    SHELL_MAX_TIMEOUT_S,
    SHELL_OUTPUT_MAX_CHARS,
    TOOLS,
    NoteMemory,
    Say,
    Shell,
    ToolCtx,
    WriteDiary,
    _truncate,
)


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


def test_shell_in_tools_list():
    from dollos.tools import TOOLS
    assert Shell in TOOLS


def test_shell_schema_has_command_and_timeout():
    schema = Shell.model_json_schema()
    assert "command" in schema["properties"]
    assert schema["properties"]["command"]["type"] == "string"
    assert "timeout_s" in schema["properties"]


def test_shell_timeout_validation_lower_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo", timeout_s=0)


def test_shell_timeout_validation_upper_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo", timeout_s=SHELL_MAX_TIMEOUT_S + 1)


def test_shell_timeout_default():
    s = Shell(command="echo")
    assert s.timeout_s == SHELL_DEFAULT_TIMEOUT_S


def test_truncate_under_cap_returns_unchanged():
    assert _truncate("hello", 100) == "hello"


def test_truncate_over_cap_inserts_marker():
    long = "a" * 500
    out = _truncate(long, 100)
    assert "[truncated 400 chars]" in out
    assert out.startswith("a" * 50)
    assert out.endswith("a" * 50)
    assert len(out) < len(long)


@pytest.mark.asyncio
async def test_shell_run_echo_returns_stdout(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="echo hi").run(ctx)
    assert "[exit 0]" in out
    assert "hi" in out


@pytest.mark.asyncio
async def test_shell_run_nonzero_exit_still_returns_str(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="false").run(ctx)
    assert "[exit 1]" in out


@pytest.mark.asyncio
async def test_shell_cwd_is_data_root(tmp_path):
    """Shell runs with cwd = ctx.memory_root.parent (i.e. data/)."""
    data_root = tmp_path / "data"
    memory_root = data_root / "memory"
    memory_root.mkdir(parents=True)
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=data_root / "transcripts",
    )
    out = await Shell(command="pwd").run(ctx)
    assert str(data_root.resolve()) in out


@pytest.mark.asyncio
async def test_shell_combines_stdout_and_stderr(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(
        command="echo to_stdout; echo to_stderr 1>&2"
    ).run(ctx)
    assert "to_stdout" in out
    assert "to_stderr" in out


@pytest.mark.asyncio
async def test_shell_timeout_returns_message_not_exception(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(command="sleep 5", timeout_s=1).run(ctx)
    assert "shell timeout" in out
    assert "1s" in out


@pytest.mark.asyncio
async def test_shell_truncates_long_output(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )
    out = await Shell(
        command=f"yes hello | head -c {SHELL_OUTPUT_MAX_CHARS * 2}"
    ).run(ctx)
    assert "[truncated" in out
