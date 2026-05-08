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
    InvokeSkill,
    NoteMemory,
    Recall,
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
    assert "我說：hello" in content
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


def test_invoke_skill_in_tools_list():
    from dollos.tools import TOOLS
    assert InvokeSkill in TOOLS


def test_invoke_skill_schema_has_name_field():
    schema = InvokeSkill.model_json_schema()
    assert "name" in schema["properties"]
    assert schema["properties"]["name"]["type"] == "string"


@pytest.mark.asyncio
async def test_invoke_skill_run_returns_body_content(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)
    body_path = bodies_dir / "my_skill.md"
    body_content = "# Steps\n\n1. Step one\n2. Step two\n"
    body_path.write_text(body_content)
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="my_skill").run(ctx)

    assert out == body_content


@pytest.mark.asyncio
async def test_invoke_skill_missing_returns_corrective_message(tmp_path):
    """ENOENT -> success-cascade str (no exception). Message includes
    '(none yet)' for empty skill dir + Shell/Recall guidance."""
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)  # empty dir
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="nope").run(ctx)
    assert "(none yet)" in out
    assert "Shell" in out
    assert "Recall" in out
    assert "nope" in out


@pytest.mark.asyncio
async def test_invoke_skill_missing_lists_existing_skills(tmp_path):
    """ENOENT -> message lists existing skills sorted by stem."""
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)
    (bodies_dir / "morning.md").write_text("...")
    (bodies_dir / "bedtime.md").write_text("...")
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="nope").run(ctx)
    assert "bedtime, morning" in out


@pytest.mark.asyncio
async def test_invoke_skill_reads_from_skill_bodies_not_skills(tmp_path):
    """Verify path goes to skill_bodies/, not skills/."""
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    skills_dir = memory_root / "skills"
    bodies_dir = memory_root / "skill_bodies"
    skills_dir.mkdir(parents=True)
    bodies_dir.mkdir(parents=True)
    (skills_dir / "x.md").write_text("ENTRY CONTENT")
    (bodies_dir / "x.md").write_text("BODY CONTENT")
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="x").run(ctx)
    assert out == "BODY CONTENT"


class _SearchableMemSearch:
    """Fake MemSearch with a configurable .search() returning canned hits."""

    def __init__(self, hits):
        self._hits = hits
        self.last_query: str | None = None
        self.last_top_k: int | None = None

    async def search(self, query, top_k=5):
        self.last_query = query
        self.last_top_k = top_k
        return self._hits

    async def index_file(self, path):  # pragma: no cover
        pass


def test_recall_in_tools_list():
    assert Recall in TOOLS


def test_recall_schema_has_query_field():
    schema = Recall.model_json_schema()
    assert "query" in schema["properties"]
    assert schema["properties"]["query"]["type"] == "string"


@pytest.mark.asyncio
async def test_recall_run_returns_bullet_list_for_hits(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _SearchableMemSearch(
        hits=[
            {"content": "user likes coffee", "score": 0.9, "source": "x.md"},
            {"content": "the sky is blue", "score": 0.8, "source": "x.md"},
        ]
    )
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await Recall(query="coffee").run(ctx)

    assert ms.last_query == "coffee"
    assert ms.last_top_k == 5
    assert "- user likes coffee" in out
    assert "- the sky is blue" in out


@pytest.mark.asyncio
async def test_recall_run_returns_no_relevant_memory_for_empty_hits(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _SearchableMemSearch(hits=[])
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await Recall(query="anything").run(ctx)

    assert out == "[no relevant memory]"
