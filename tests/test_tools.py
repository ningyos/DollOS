"""Tests for Tool classes (Say, NoteMemory) and ToolCtx."""

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.ipc.messages import TextChunk
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import (
    MAIN_TOOLS,
    SUB_TOOLS,
    GrepToolOutput,
    InvokeSkill,
    NoteMemory,
    ReadToolOutput,
    Recall,
    Say,
    Shell,
    SubagentToolCtx,
    ToolCtx,
    WriteDiary,
)


class _FakeMemSearch:
    """Fake MemSearch — captures index_file calls."""

    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


def _make_ctx(
    tmp_path: Path,
    tool_output_store: ToolOutputStore | None = None,
) -> tuple[ToolCtx, _FakeMemSearch, asyncio.Queue]:
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=tool_output_store or ToolOutputStore(tmp_path / "tool_outputs"),
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
    assert Say in MAIN_TOOLS
    assert NoteMemory in MAIN_TOOLS


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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
    assert WriteDiary in MAIN_TOOLS


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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
    assert Shell in MAIN_TOOLS


def test_shell_schema_has_command_and_timeout_s():
    schema = Shell.model_json_schema()
    assert "command" in schema["properties"]
    assert schema["properties"]["command"]["type"] == "string"
    assert "timeout_s" in schema["properties"]
    assert schema["properties"]["timeout_s"]["minimum"] == 1
    assert schema["properties"]["timeout_s"]["maximum"] == 600


def test_shell_timeout_s_required():
    with pytest.raises(ValidationError):
        Shell(command="echo hi")  # timeout_s missing


def test_shell_timeout_s_validation_lower_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo hi", timeout_s=0)


def test_shell_timeout_s_validation_upper_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo hi", timeout_s=601)


@pytest.mark.asyncio
async def test_shell_tool_delegates_to_runner(tmp_path):
    """Shell.run dispatches via ShellRunner.spawn and returns immediate ack."""
    from unittest.mock import MagicMock

    runner = MagicMock()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        shell_runner=runner,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    out = await Shell(command="echo hi", timeout_s=30).run(ctx)
    runner.spawn.assert_called_once_with(
        command="echo hi", timeout_s=30, response_sink=sink,
    )
    assert "shell" in out.lower()
    assert "結果" in out  # result-will-arrive language


def test_invoke_skill_in_tools_list():
    assert InvokeSkill in MAIN_TOOLS


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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
    assert Recall in MAIN_TOOLS


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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
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
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )

    out = await Recall(query="anything").run(ctx)

    assert out == "[no relevant memory]"


# ---------- Recall date filter / formatting (2026-05-10) ----------


def _ctx_with_search(tmp_path, hits):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _SearchableMemSearch(hits=hits)
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    return ctx


@pytest.mark.asyncio
async def test_recall_filters_by_since(tmp_path):
    from datetime import datetime as _dt

    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "old fact", "score": 0.9,
             "source": "shared/2026-05-08.md"},
            {"content": "new fact", "score": 0.8,
             "source": "shared/2026-05-10.md"},
        ],
    )
    out = await Recall(
        query="x", since=_dt(2026, 5, 10, 0, 0, 0)
    ).run(ctx)
    assert "new fact" in out
    assert "old fact" not in out


@pytest.mark.asyncio
async def test_recall_filters_by_until(tmp_path):
    from datetime import datetime as _dt

    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "old fact", "score": 0.9,
             "source": "shared/2026-05-08.md"},
            {"content": "new fact", "score": 0.8,
             "source": "shared/2026-05-10.md"},
        ],
    )
    out = await Recall(
        query="x", until=_dt(2026, 5, 9, 23, 59, 59)
    ).run(ctx)
    assert "old fact" in out
    assert "new fact" not in out


@pytest.mark.asyncio
async def test_recall_filters_by_both(tmp_path):
    from datetime import datetime as _dt

    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "older", "score": 0.9,
             "source": "shared/2026-05-01.md"},
            {"content": "middle", "score": 0.8,
             "source": "shared/2026-05-08.md"},
            {"content": "newer", "score": 0.7,
             "source": "shared/2026-05-10.md"},
        ],
    )
    out = await Recall(
        query="x",
        since=_dt(2026, 5, 5, 0, 0, 0),
        until=_dt(2026, 5, 9, 0, 0, 0),
    ).run(ctx)
    assert "middle" in out
    assert "older" not in out
    assert "newer" not in out


@pytest.mark.asyncio
async def test_recall_no_filter_returns_all(tmp_path):
    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "a", "score": 0.9,
             "source": "shared/2026-05-08.md"},
            {"content": "b", "score": 0.8,
             "source": "shared/2026-05-10.md"},
        ],
    )
    out = await Recall(query="x").run(ctx)
    assert "a" in out
    assert "b" in out


@pytest.mark.asyncio
async def test_recall_skips_hits_without_date_when_filtering(tmp_path):
    from datetime import datetime as _dt

    hits = [
        {"content": "no-date", "score": 0.9, "source": "shared/no-date.md"},
        {"content": "dated", "score": 0.8,
         "source": "shared/2026-05-10.md"},
    ]
    # With filter: undated hit excluded.
    ctx_filtered = _ctx_with_search(tmp_path, hits=list(hits))
    out_filtered = await Recall(
        query="x", since=_dt(2026, 5, 10, 0, 0, 0)
    ).run(ctx_filtered)
    assert "dated" in out_filtered
    assert "no-date" not in out_filtered

    # Without filter: undated hit included.
    ctx_unfiltered = _ctx_with_search(tmp_path, hits=list(hits))
    out_all = await Recall(query="x").run(ctx_unfiltered)
    assert "no-date" in out_all
    assert "dated" in out_all


@pytest.mark.asyncio
async def test_recall_formats_hits_with_date_prefix(tmp_path):
    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "coffee fact", "score": 0.9,
             "source": "shared/2026-05-08.md"},
        ],
    )
    out = await Recall(query="x").run(ctx)
    assert "- 2026-05-08 coffee fact" in out


@pytest.mark.asyncio
async def test_write_diary_uses_seconds_in_timestamp(tmp_path):
    import re as _re

    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    await WriteDiary(content="今天好累").run(ctx)
    expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    content = expected.read_text()
    assert _re.search(r"## 日記 \(\d{2}:\d{2}:\d{2}\)", content)


# ---------- SpawnSubagent / Report ----------


def test_main_tools_includes_spawn_subagent_not_report():
    from dollos.tools import MAIN_TOOLS, Report, SpawnSubagent

    assert SpawnSubagent in MAIN_TOOLS
    assert Report not in MAIN_TOOLS


def test_sub_tools_includes_report_not_say_or_spawn():
    from dollos.tools import SUB_TOOLS, Report, Say, SpawnSubagent

    assert Report in SUB_TOOLS
    assert Say not in SUB_TOOLS
    assert SpawnSubagent not in SUB_TOOLS


def test_spawn_subagent_schema_has_task_and_timeout_s():
    from dollos.tools import SpawnSubagent

    schema = SpawnSubagent.model_json_schema()
    assert "task" in schema["properties"]
    assert "timeout_s" in schema["properties"]
    assert "task" in schema["required"]
    assert "timeout_s" in schema["required"]
    assert schema["properties"]["timeout_s"]["minimum"] == 1
    assert schema["properties"]["timeout_s"]["maximum"] == 600


def test_report_schema_has_status_summary_details():
    from dollos.tools import Report

    schema = Report.model_json_schema()
    for f in ("status", "summary", "details"):
        assert f in schema["properties"]
        assert f in schema["required"]
    assert set(schema["properties"]["status"]["enum"]) == {"ok", "incomplete"}


@pytest.mark.asyncio
async def test_spawn_subagent_invokes_runner_and_returns_dispatch_msg(tmp_path):
    """SpawnSubagent.run delegates to ctx.subagent_runner.spawn and returns a
    confirmation string mentioning dispatch + the task + the timeout."""
    from dollos.tools import SpawnSubagent

    captured: list[dict] = []

    class _FakeRunner:
        def spawn(self, **kwargs):
            captured.append(kwargs)

    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        subagent_runner=_FakeRunner(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )

    out = await SpawnSubagent(
        task="search transcripts for coffee", timeout_s=30
    ).run(ctx)

    assert len(captured) == 1
    kw = captured[0]
    assert kw["task"] == "search transcripts for coffee"
    assert kw["timeout_s"] == 30
    assert kw["response_sink"] is sink
    assert "sub_id" in kw and len(kw["sub_id"]) >= 4
    assert "dispatched" in out
    assert "30" in out


@pytest.mark.asyncio
async def test_report_stashes_args_into_ctx_and_returns_none(tmp_path):
    """Report.run side-effects ctx.subagent_report and returns None
    (cascade-ending semantics)."""
    from dollos.tools import Report

    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    ctx = SubagentToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    assert ctx.subagent_report is None

    out = await Report(
        status="ok", summary="done", details="data here"
    ).run(ctx)

    assert out is None
    assert ctx.subagent_report == {
        "status": "ok",
        "summary": "done",
        "details": "data here",
    }


# ----- WriteSchedule (Phase 1 schedule) -----


@pytest.mark.asyncio
async def test_write_schedule_writes_toml_file(tmp_path):
    from datetime import time as _time

    from dollos.schedule import load_schedule
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    tool = WriteSchedule(entries=[
        ScheduleEntryArg(time="07:30:00", intent="morning"),
        ScheduleEntryArg(time="11:30:00", intent="lunch"),
    ])
    out = await tool.run(ctx)
    assert "2 entries" in out

    today_str = f"{date.today():%Y-%m-%d}"
    path = tmp_path / "schedule" / f"{today_str}.toml"
    assert path.exists()
    sched = load_schedule(path)
    assert sched is not None
    assert sched.entries[0].time == _time(7, 30)
    assert sched.entries[1].intent == "lunch"


@pytest.mark.asyncio
async def test_write_schedule_overwrites_existing(tmp_path):
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(entries=[
        ScheduleEntryArg(time="07:00:00", intent="old1"),
        ScheduleEntryArg(time="08:00:00", intent="old2"),
    ]).run(ctx)
    await WriteSchedule(entries=[
        ScheduleEntryArg(time="09:00:00", intent="new"),
    ]).run(ctx)

    today_str = f"{date.today():%Y-%m-%d}"
    path = tmp_path / "schedule" / f"{today_str}.toml"
    text = path.read_text()
    assert "old1" not in text and "old2" not in text
    assert "new" in text


@pytest.mark.asyncio
async def test_write_schedule_writes_markdown_summary(tmp_path):
    """gap #3 — also writes a markdown summary into shared so Recall can find."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(entries=[
        ScheduleEntryArg(time="07:30:00", intent="morning"),
    ]).run(ctx)

    today_str = f"{date.today():%Y-%m-%d}"
    md_path = tmp_path / "shared" / f"{today_str}.md"
    assert md_path.exists()
    body = md_path.read_text()
    assert "## 計劃" in body
    assert "07:30:00 — morning" in body


@pytest.mark.asyncio
async def test_write_schedule_indexes_markdown_with_memsearch(tmp_path):
    """gap #3 — markdown summary is indexed in memsearch so Recall surfaces it."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(entries=[
        ScheduleEntryArg(time="07:30:00", intent="morning"),
    ]).run(ctx)

    today_str = f"{date.today():%Y-%m-%d}"
    md_path = tmp_path / "shared" / f"{today_str}.md"
    assert md_path in ms.indexed


def test_write_schedule_validates_time_format():
    """gap #6 — ScheduleEntryArg rejects non-HH:MM:SS strings."""
    from dollos.tools import ScheduleEntryArg

    with pytest.raises(ValidationError):
        ScheduleEntryArg(time="not a time", intent="x")
    with pytest.raises(ValidationError):
        ScheduleEntryArg(time="25:00:00", intent="x")


@pytest.mark.asyncio
async def test_spawn_monitor_delegates_to_runner(tmp_path):
    from dollos.tools import SpawnMonitor, ToolCtx
    from unittest.mock import MagicMock

    runner = MagicMock()
    runner.spawn.return_value = "mon-1"
    sink: asyncio.Queue = asyncio.Queue()
    ctx = ToolCtx(
        sink=sink,
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    out = await SpawnMonitor(
        command="tail -F /var/log/x",
        match_regex=r"ERROR",
        rate_limit_s=60,
    ).run(ctx)
    runner.spawn.assert_called_once_with(
        command="tail -F /var/log/x",
        match_regex=r"ERROR",
        rate_limit_s=60,
        response_sink=sink,
    )
    assert "mon-1" in out


@pytest.mark.asyncio
async def test_remove_monitor_delegates(tmp_path):
    from dollos.tools import RemoveMonitor, ToolCtx
    from unittest.mock import AsyncMock, MagicMock

    runner = MagicMock()
    runner.remove = AsyncMock(return_value=True)
    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    out = await RemoveMonitor(monitor_id="mon-3").run(ctx)
    runner.remove.assert_awaited_once_with("mon-3")
    assert "mon-3" in out
    assert "removed" in out.lower() or "kill" in out.lower()


@pytest.mark.asyncio
async def test_remove_monitor_unknown_id(tmp_path):
    from dollos.tools import RemoveMonitor, ToolCtx
    from unittest.mock import AsyncMock, MagicMock

    runner = MagicMock()
    runner.remove = AsyncMock(return_value=False)
    ctx = ToolCtx(
        sink=asyncio.Queue(),
        memory_root=tmp_path,
        memsearch=MagicMock(),
        transcripts_root=tmp_path,
        monitor_runner=runner,
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    out = await RemoveMonitor(monitor_id="mon-999").run(ctx)
    assert "mon-999" in out
    assert "unknown" in out.lower() or "not found" in out.lower()


# ---------- ReadToolOutput ----------


@pytest.mark.asyncio
async def test_read_tool_output_returns_slice(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"row {i}" for i in range(50)))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    # Replace the ctx's store with the one we just wrote to.
    ctx.tool_output_store = store

    tool = ReadToolOutput(id=output_id, offset=10, limit=5)
    result = await tool.run(ctx)

    assert "row 10" in result
    assert "row 14" in result
    assert "row 15" not in result
    assert "lines 10–15 of 50" in result


@pytest.mark.asyncio
async def test_read_tool_output_invalid_id(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    ctx, _ms, _sink = _make_ctx(tmp_path)
    ctx.tool_output_store = store
    tool = ReadToolOutput(id="../etc/passwd", offset=0, limit=10)
    with pytest.raises(ValueError):
        await tool.run(ctx)


@pytest.mark.asyncio
async def test_read_tool_output_not_found(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    ctx, _ms, _sink = _make_ctx(tmp_path)
    ctx.tool_output_store = store
    tool = ReadToolOutput(id="out-deadbeef", offset=0, limit=10)
    with pytest.raises(FileNotFoundError):
        await tool.run(ctx)


# ---------- GrepToolOutput ----------


@pytest.mark.asyncio
async def test_grep_tool_output_finds_matches(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    text = "error: foo\nok\nerror: bar\nok\nERROR: baz\n"
    output_id = store.write(text)
    ctx, _ms, _sink = _make_ctx(tmp_path, tool_output_store=store)

    tool = GrepToolOutput(id=output_id, pattern=r"error:", max_matches=10)
    result = await tool.run(ctx)

    assert "line 0: error: foo" in result
    assert "line 2: error: bar" in result
    assert "ERROR" not in result   # case-sensitive by default


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\nworld\n")
    ctx, _ms, _sink = _make_ctx(tmp_path, tool_output_store=store)
    tool = GrepToolOutput(id=output_id, pattern=r"^nothing$", max_matches=10)
    result = await tool.run(ctx)
    assert "no matches" in result


@pytest.mark.asyncio
async def test_grep_invalid_regex_raises(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("hello\n")
    ctx, _ms, _sink = _make_ctx(tmp_path, tool_output_store=store)
    # Trailing unbalanced paren — re.error at run time
    tool = GrepToolOutput(id=output_id, pattern=r"(", max_matches=10)
    with pytest.raises(re.error):
        await tool.run(ctx)

