"""Tests for Tool classes and MindCtx."""

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_state import MindState, Mood
from dollos.mind.sink_resolver import SinkResolver
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import (
    SUB_TOOLS,
    InvokeSkill,
    NoteMemory,
    ReadToolOutput,
    Recall,
    Shell,
    WriteDiary,
    SetFocus,
    OpenLoop,
    CloseLoop,
)


class _FakeMemSearch:
    """Fake MemSearch — captures index_file calls."""

    def __init__(self) -> None:
        self.indexed: list[Path] = []

    async def index_file(self, path):
        self.indexed.append(Path(path))


class _FakeShellRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def spawn(self, **kwargs):
        self.calls.append(kwargs)


class _FakeWorkflowRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def spawn(self, *, workflow_id, tasks, synthesis=None, mode="map_reduce", timeout_s=600, response_sink=None):
        self.calls.append(dict(workflow_id=workflow_id, tasks=tasks, synthesis=synthesis, mode=mode, timeout_s=timeout_s))


def _fake_sink_resolver(sink=None):
    """Returns a SinkResolver pre-registered with the given sink (or a fresh queue)."""
    r = SinkResolver()
    if sink is not None:
        r.register(sink)
    return r


def _make_ctx(
    tmp_path: Path,
    tool_output_store: ToolOutputStore | None = None,
    state: MindState | None = None,
    sink: asyncio.Queue | None = None,
    shell_runner=None,
    workflow_runner=None,
) -> tuple[MindCtx, _FakeMemSearch, asyncio.Queue]:
    if sink is None:
        sink = asyncio.Queue()
    ms = _FakeMemSearch()
    mind_state = state or MindState()
    ctx = MindCtx(
        mind_state=mind_state,
        memsearch=ms,
        memory_root=tmp_path,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=_fake_sink_resolver(sink),
        tool_output_store=tool_output_store or ToolOutputStore(tmp_path / "tool_outputs"),
        shell_runner=shell_runner or _FakeShellRunner(),
        workflow_runner=workflow_runner or _FakeWorkflowRunner(),
    )
    return ctx, ms, sink


@pytest.mark.asyncio
async def test_note_memory_run_appends_bullet_to_daily_file(tmp_path):
    ctx, _ms, _sink = _make_ctx(tmp_path)
    note = NoteMemory(text="主人喜歡咖啡")
    await note.run(ctx)

    expected_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected_path.exists()
    content = expected_path.read_text()
    # New format: H2 heading with context tags, then body
    assert "主人喜歡咖啡" in content
    assert content.lstrip().startswith("## ")
    assert "tod:" in content and "dow:" in content


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
    assert "new fact" in content
    # The new entry appends an H2 section.
    assert "## " in content


@pytest.mark.asyncio
async def test_write_diary_writes_markdown_section_and_indexes(tmp_path):
    ctx, ms, _sink = _make_ctx(tmp_path)
    diary = WriteDiary(content="今天我學會了 transcript 跟 diary。")
    await diary.run(ctx)

    expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    assert expected.exists()
    content = expected.read_text()
    assert re.search(r"^## .+ Diary$", content, re.M)
    assert content.count("## ") >= 1
    assert "今天我學會了 transcript 跟 diary。" in content
    assert ms.indexed and Path(ms.indexed[-1]) == expected


def test_shell_timeout_s_has_default():
    # Omitted timeout_s waits: the default is DOLL_WAIT_MAX_S, so Doll's
    # Shell returns in the same iteration instead of going fire-and-forget
    # (rerun report 2026-10-29 發現 2).
    from dollos.tools import DOLL_WAIT_MAX_S
    s = Shell(command="echo hi")
    assert s.timeout_s == DOLL_WAIT_MAX_S == 30
    desc = Shell.model_fields["timeout_s"].description
    assert "Default 30s" in desc and "Default 60s" not in desc


def test_shell_timeout_s_validation_lower_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo hi", timeout_s=0)


def test_shell_timeout_s_validation_upper_bound():
    with pytest.raises(ValidationError):
        Shell(command="echo hi", timeout_s=601)


@pytest.mark.asyncio
async def test_shell_tool_delegates_to_runner(tmp_path):
    """Doll's long Shell (timeout_s > DOLL_WAIT_MAX_S) dispatches via
    ShellRunner.spawn and returns an immediate ack."""
    runner = _FakeShellRunner()
    ctx, _ms, _sink = _make_ctx(tmp_path, shell_runner=runner)
    out = await Shell(command="echo hi", timeout_s=31).run(ctx)
    assert len(runner.calls) == 1
    kw = runner.calls[0]
    assert kw["command"] == "echo hi"
    assert kw["timeout_s"] == 31
    assert kw["response_sink"] is None  # Task 8 wires perception_queue here
    assert "shell" in out.lower()
    assert "comes back as a new event" in out  # result-will-arrive language


# ----- Shell in a workflow worker waits for its result (plan 2026-10-28 decision 2) -----


def _shell_ctx(tmp_path: Path, *, worker: bool, cwd: Path | None = None):
    """A ctx with a real ShellRunner wired to its own queue: a worker agent's
    (``workflow_runner=None``, as run_agent builds it) or Doll's."""
    from dollos.mind.perception_queue import PerceptionQueue
    from dollos.shell_runner import ShellRunner

    store = ToolOutputStore(tmp_path / "tool_outputs")
    queue = PerceptionQueue()
    runner = ShellRunner(
        cwd=cwd or tmp_path, perception_queue=queue, tool_output_store=store,
    )
    ctx = MindCtx(
        mind_state=MindState(),
        memsearch=_FakeMemSearch(),
        memory_root=tmp_path,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=_fake_sink_resolver(),
        tool_output_store=store,
        shell_runner=runner,
        workflow_runner=None if worker else _FakeWorkflowRunner(),
    )
    return ctx, queue


async def _shell_results(queue, settle: float = 0.3) -> list:
    found = []
    for _ in range(int(settle / 0.05)):
        found.extend(
            p for p in await queue.drain(timeout_s=0.05) if p.kind == "ToolResultArrived"
        )
    return found


@pytest.mark.asyncio
async def test_worker_shell_returns_output_and_doll_queue_stays_empty(tmp_path):
    ctx, queue = _shell_ctx(tmp_path, worker=True)
    out = await Shell(command="printf 'alpha\\nbeta\\n'", timeout_s=10).run(ctx)
    assert isinstance(out, str)
    assert out.startswith("Shell done [ok]: exit 0")
    assert "printf" in out  # the command, so parallel results stay apart
    assert "Inside a workflow the result comes straight back" in out
    assert "no ToolResultArrived" in out
    assert out.splitlines()[-2:] == ["alpha", "beta"]
    assert re.search(r"output_id out-[0-9a-f]{8}, line count 2", out)
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_worker_shell_long_output_previews_and_pages_by_id(tmp_path):
    ctx, queue = _shell_ctx(tmp_path, worker=True)
    out = await Shell(command="seq 1 25", timeout_s=10).run(ctx)
    lines = out.splitlines()
    out_id = re.search(r"output_id (out-[0-9a-f]{8})", out).group(1)
    assert lines[-11:] == [
        *[str(i) for i in range(1, 7)],
        f"… 15 lines not shown (ReadToolOutput id={out_id} offset=6 limit=15) …",
        *[str(i) for i in range(22, 26)],
    ]
    assert "11" not in lines
    assert "line count 25" in out and "ReadToolOutput" in out
    # The worker pages past the preview with its own SUB_TOOLS.
    assert ReadToolOutput in SUB_TOOLS
    more = await ReadToolOutput(id=out_id, offset=10, limit=5).run(ctx)
    assert more.splitlines()[1:] == ["11", "12", "13", "14", "15"]
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_worker_shell_long_command_keeps_output_id_inside_the_cut(tmp_path):
    """The head line echoes only the start of the command: a long inline
    script must not push the output_id and the preview past the runner's
    TOOL_MESSAGE_MAX_CHARS cut."""
    from dollos.harness.runner import TOOL_MESSAGE_MAX_CHARS

    ctx, _queue = _shell_ctx(tmp_path, worker=True)
    command = "echo RESULT_LINE; : '" + "x\n" * TOOL_MESSAGE_MAX_CHARS + "'"
    out = await Shell(command=command, timeout_s=10).run(ctx)
    kept = out[:TOOL_MESSAGE_MAX_CHARS]
    assert "echo RESULT_LINE" in kept.splitlines()[0]
    assert re.search(r"output_id out-[0-9a-f]{8}, line count 1:", kept)
    assert kept.splitlines()[-1] == "RESULT_LINE"


@pytest.mark.asyncio
async def test_worker_shell_nonzero_reads_as_failure_but_is_not_a_failed_call(tmp_path):
    """A nonzero exit is the command's answer (diff found differences, grep
    found nothing): the text says it failed, the call itself succeeded, so
    the worker's stuck_tool rule does not count it."""
    from dollos.harness.executor import dispatch_one

    ctx, queue = _shell_ctx(tmp_path, worker=True)
    result = await dispatch_one(
        "Shell", {"command": "echo nope; exit 3", "timeout_s": 10}, ctx,
        {"Shell": Shell},
    )
    assert result.success is True
    assert result.detail.startswith("Shell failed [nonzero]: exit 3")
    assert result.detail.splitlines()[-1] == "nope"
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_worker_shell_timeout_reads_as_failure(tmp_path):
    from dollos.harness.executor import dispatch_one

    ctx, queue = _shell_ctx(tmp_path, worker=True)
    result = await dispatch_one(
        "Shell", {"command": "sleep 5", "timeout_s": 1}, ctx, {"Shell": Shell},
    )
    assert result.success is True
    assert result.detail.startswith("Shell failed [timeout]: killed after 1s")
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_worker_shell_refuses_a_timeout_at_the_per_agent_cap(tmp_path):
    """A worker's whole run is cut off at MAX_AGENT_TIMEOUT_S, and then
    nothing it found is kept: a Shell whose timeout_s reaches the cap could
    never come back [timeout]. It is refused without running."""
    from dollos.harness.executor import dispatch_one
    from dollos.workflow import MAX_AGENT_TIMEOUT_S

    ctx, queue = _shell_ctx(tmp_path, worker=True)
    marker = tmp_path / "ran"
    result = await dispatch_one(
        "Shell", {"command": f"touch {marker}", "timeout_s": MAX_AGENT_TIMEOUT_S},
        ctx, {"Shell": Shell},
    )
    assert result.success is False
    assert result.detail.startswith(f"Shell refused: timeout_s={MAX_AGENT_TIMEOUT_S}")
    assert f"cut off after {MAX_AGENT_TIMEOUT_S}s" in result.detail
    await asyncio.sleep(0.2)
    assert not marker.exists()
    assert await _shell_results(queue) == []

    result = await dispatch_one(
        "Shell", {"command": f"touch {marker}", "timeout_s": MAX_AGENT_TIMEOUT_S - 1},
        ctx, {"Shell": Shell},
    )
    assert result.success is True
    assert marker.exists()


@pytest.mark.asyncio
async def test_doll_shell_keeps_timeouts_past_the_per_agent_cap(tmp_path):
    """The cap is a worker's: Doll's Shell is not inside a workflow."""
    from dollos.workflow import MAX_AGENT_TIMEOUT_S

    ctx, queue = _shell_ctx(tmp_path, worker=False)
    out = await Shell(command="printf hello", timeout_s=MAX_AGENT_TIMEOUT_S * 2).run(ctx)
    assert "comes back as a new event" in out
    assert len(await _shell_results(queue, settle=1.0)) == 1
    await ctx.shell_runner.stop()


@pytest.mark.asyncio
async def test_worker_shell_runner_error_is_a_failed_call(tmp_path):
    from dollos.harness.executor import dispatch_one

    ctx, queue = _shell_ctx(tmp_path, worker=True, cwd=tmp_path / "missing")
    result = await dispatch_one(
        "Shell", {"command": "echo hi", "timeout_s": 10}, ctx, {"Shell": Shell},
    )
    assert result.success is False
    assert result.detail.startswith("Shell failed [error]: runner error:")
    assert await _shell_results(queue) == []


# ----- Doll's short Shell waits too (plan 2026-10-29 decision 1) -----


def test_doll_wait_limit_is_30s():
    from dollos.tools import DOLL_WAIT_MAX_S

    assert DOLL_WAIT_MAX_S == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [5, 30])
async def test_doll_short_shell_returns_result_and_queue_stays_empty(tmp_path, timeout_s):
    ctx, queue = _shell_ctx(tmp_path, worker=False)
    out = await Shell(command="printf 'alpha\\nbeta\\n'", timeout_s=timeout_s).run(ctx)
    assert isinstance(out, str)
    assert out.startswith("Shell done [ok]: exit 0")
    assert "printf" in out
    assert "workflow" not in out.lower()
    assert "The result is here; no ToolResultArrived event will follow." in out
    assert out.splitlines()[-2:] == ["alpha", "beta"]
    assert re.search(r"output_id out-[0-9a-f]{8}, line count 2", out)
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_doll_short_shell_long_output_shows_head_and_tail(tmp_path):
    """Rerun 2026-10-29 3.1 rep 3: the answer was on the last of 42 lines
    and only the first 10 were shown."""
    ctx, _queue = _shell_ctx(tmp_path, worker=False)
    out = await Shell(command="seq 1 42", timeout_s=10).run(ctx)
    lines = out.splitlines()
    out_id = re.search(r"output_id (out-[0-9a-f]{8})", out).group(1)
    assert lines[2] == (
        f"output_id {out_id}, line count 42; the first 6 and last 4 lines "
        f"below, ReadToolOutput for the rest:"
    )
    assert lines[3:] == [
        *[str(i) for i in range(1, 7)],
        f"… 32 lines not shown (ReadToolOutput id={out_id} offset=6 limit=32) …",
        *[str(i) for i in range(39, 43)],
    ]


@pytest.mark.asyncio
async def test_doll_short_shell_long_lines_keep_the_tail_inside_the_cut(tmp_path):
    """Long head lines must not push the tail past the runner's
    TOOL_MESSAGE_MAX_CHARS cut: each preview line is capped."""
    from dollos.harness.runner import TOOL_MESSAGE_MAX_CHARS

    ctx, _queue = _shell_ctx(tmp_path, worker=False)
    command = (
        "for i in $(seq 1 41); do printf 'x%.0s' $(seq 1 3000); echo; done; "
        "echo ANSWER_37"
    )
    out = await Shell(command=command, timeout_s=10).run(ctx)
    assert len(out) < TOOL_MESSAGE_MAX_CHARS // 2
    kept = out[:TOOL_MESSAGE_MAX_CHARS]
    assert kept.splitlines()[-1] == "ANSWER_37"
    assert "… (line cut, 3000 chars)" in kept


@pytest.mark.asyncio
async def test_doll_short_shell_ten_lines_shown_whole(tmp_path):
    ctx, _queue = _shell_ctx(tmp_path, worker=False)
    out = await Shell(command="seq 1 10", timeout_s=10).run(ctx)
    lines = out.splitlines()
    assert re.fullmatch(r"output_id out-[0-9a-f]{8}, line count 10:", lines[2])
    assert lines[3:] == [str(i) for i in range(1, 11)]


@pytest.mark.asyncio
async def test_doll_short_shell_timeout_reads_as_failure(tmp_path):
    """Like a worker's: a [timeout] is the command's answer, not a failed call."""
    from dollos.harness.executor import dispatch_one

    ctx, queue = _shell_ctx(tmp_path, worker=False)
    result = await dispatch_one(
        "Shell", {"command": "sleep 5", "timeout_s": 1}, ctx, {"Shell": Shell},
    )
    assert result.success is True
    assert result.detail.startswith("Shell failed [timeout]: killed after 1s")
    assert "workflow" not in result.detail.lower()
    assert await _shell_results(queue) == []


@pytest.mark.asyncio
async def test_doll_shell_still_enqueues_the_result(tmp_path):
    """A Shell longer than DOLL_WAIT_MAX_S stays fire-and-forget for Doll."""
    ctx, queue = _shell_ctx(tmp_path, worker=False)
    out = await Shell(command="printf hello", timeout_s=60).run(ctx)
    assert "comes back as a new event" in out
    results = await _shell_results(queue, settle=1.0)
    assert len(results) == 1
    assert results[0].data["tool"] == "Shell"
    assert results[0].data["status"] == "ok"
    assert "hello" in results[0].data["summary"]
    await ctx.shell_runner.stop()


@pytest.mark.asyncio
async def test_invoke_skill_run_returns_body_content(tmp_path):
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)
    body_path = bodies_dir / "my_skill.md"
    body_content = "# Steps\n\n1. Step one\n2. Step two\n"
    body_path.write_text(body_content)
    state = MindState()
    ctx = MindCtx(
        mind_state=state,
        memsearch=_FakeMemSearch(),
        memory_root=memory_root,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=_fake_sink_resolver(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        shell_runner=_FakeShellRunner(),
        workflow_runner=_FakeWorkflowRunner(),
    )

    out = await InvokeSkill(name="my_skill").run(ctx)

    assert out == body_content


@pytest.mark.asyncio
async def test_invoke_skill_missing_returns_corrective_message(tmp_path):
    """ENOENT -> success-cascade str (no exception). Message says the skill
    doesn't exist and lists what does ('(none yet)' for an empty skill dir);
    it gives no advice on what to do instead (plan 2026-10-19 Task 7)."""
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)  # empty dir
    ctx, _ms, _sink = _make_ctx(tmp_path)
    ctx.memory_root = memory_root

    out = await InvokeSkill(name="nope").run(ctx)
    assert out == "Skill 'nope' doesn't exist. Skills available now: (none yet)"


@pytest.mark.asyncio
async def test_invoke_skill_missing_lists_existing_skills(tmp_path):
    """ENOENT -> message lists existing skills sorted by stem."""
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)
    (bodies_dir / "morning.md").write_text("...")
    (bodies_dir / "bedtime.md").write_text("...")
    ctx, _ms, _sink = _make_ctx(tmp_path)
    ctx.memory_root = memory_root

    out = await InvokeSkill(name="nope").run(ctx)
    assert "bedtime, morning" in out


@pytest.mark.asyncio
async def test_invoke_skill_reads_from_skill_bodies_not_skills(tmp_path):
    """Verify path goes to skill_bodies/, not skills/."""
    memory_root = tmp_path / "memory"
    skills_dir = memory_root / "skills"
    bodies_dir = memory_root / "skill_bodies"
    skills_dir.mkdir(parents=True)
    bodies_dir.mkdir(parents=True)
    (skills_dir / "x.md").write_text("ENTRY CONTENT")
    (bodies_dir / "x.md").write_text("BODY CONTENT")
    ctx, _ms, _sink = _make_ctx(tmp_path)
    ctx.memory_root = memory_root

    out = await InvokeSkill(name="x").run(ctx)
    assert out == "BODY CONTENT"


class _SearchableMemSearch:
    """Fake MemSearch with a configurable .search() returning canned hits."""

    def __init__(self, hits):
        self._hits = hits
        self.last_query: str | None = None
        self.last_top_k: int | None = None

    async def search(self, query, top_k=5, **_kw):
        self.last_query = query
        self.last_top_k = top_k
        return self._hits

    async def index_file(self, path):  # pragma: no cover
        pass


def _ctx_with_search(tmp_path, hits):
    ms = _SearchableMemSearch(hits=hits)
    state = MindState()
    ctx = MindCtx(
        mind_state=state,
        memsearch=ms,
        memory_root=tmp_path,
        transcripts_root=tmp_path / "transcripts",
        sink_resolver=_fake_sink_resolver(),
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        shell_runner=_FakeShellRunner(),
        workflow_runner=_FakeWorkflowRunner(),
    )
    return ctx


@pytest.mark.asyncio
async def test_recall_run_returns_bullet_list_for_hits(tmp_path):
    ctx = _ctx_with_search(
        tmp_path,
        hits=[
            {"content": "user likes coffee", "score": 0.9, "source": "x.md"},
            {"content": "the sky is blue", "score": 0.8, "source": "x.md"},
        ],
    )
    out = await Recall(query="coffee").run(ctx)

    assert ctx.memsearch.last_query == "coffee"
    assert ctx.memsearch.last_top_k == 5
    assert "- user likes coffee" in out
    assert "- the sky is blue" in out


@pytest.mark.asyncio
async def test_recall_run_returns_no_relevant_memory_for_empty_hits(tmp_path):
    ctx = _ctx_with_search(tmp_path, hits=[])
    out = await Recall(query="anything").run(ctx)
    assert out == "[no relevant memory]"


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

    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteDiary(content="今天好累").run(ctx)
    expected = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    content = expected.read_text()
    # New heading format embeds full timestamp; "Diary" is suffixed after the tag block.
    assert _re.search(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.* Diary$", content, _re.M)


# ---------- SpawnWorkflow / Report ----------


def test_main_tools_includes_spawn_workflow_not_report():
    from dollos.tools import MAIN_TOOLS, Report, SpawnWorkflow

    assert SpawnWorkflow in MAIN_TOOLS
    assert Report not in MAIN_TOOLS


def test_sub_tools_includes_report_not_spawn_workflow():
    from dollos.tools import SUB_TOOLS, Report, SpawnWorkflow

    assert Report in SUB_TOOLS
    assert SpawnWorkflow not in SUB_TOOLS


def test_spawn_workflow_tasks_max_length():
    from dollos.tools import SpawnWorkflow, WorkflowTask
    from pydantic import ValidationError

    # min_length=1: empty list rejected
    with pytest.raises(ValidationError):
        SpawnWorkflow(tasks=[])
    # max_length=16: list of 17 rejected
    with pytest.raises(ValidationError):
        SpawnWorkflow(tasks=[WorkflowTask(task=f"t{i}") for i in range(17)])
    # exactly 16 is ok
    sw = SpawnWorkflow(tasks=[WorkflowTask(task=f"t{i}") for i in range(16)])
    assert len(sw.tasks) == 16


@pytest.mark.asyncio
async def test_spawn_workflow_invokes_runner_and_returns_dispatch_msg(tmp_path):
    """SpawnWorkflow.run delegates to ctx.workflow_runner.spawn and returns a
    confirmation string mentioning dispatch."""
    from dollos.tools import SpawnWorkflow, WorkflowTask

    runner = _FakeWorkflowRunner()
    ctx, _ms, _sink = _make_ctx(tmp_path, workflow_runner=runner)

    out = await SpawnWorkflow(
        tasks=[WorkflowTask(task="search transcripts for coffee"),
               WorkflowTask(task="check memory for tea")],
        mode="map_reduce",
        synthesis="combine findings",
        timeout_s=120,
    ).run(ctx)

    assert len(runner.calls) == 1
    kw = runner.calls[0]
    assert kw["tasks"] == ["search transcripts for coffee", "check memory for tea"]
    assert kw["mode"] == "map_reduce"
    assert kw["synthesis"] == "combine findings"
    assert kw["timeout_s"] == 120
    assert "workflow_id" in kw and len(kw["workflow_id"]) >= 4
    # dispatch ack string mentions something about workflow/dispatch
    assert "dispatched" in out and "comes back as a new event" in out


@pytest.mark.asyncio
async def test_report_stashes_args_into_ctx_and_returns_none(tmp_path):
    """Report.run side-effects ctx.agent_report and returns None
    (cascade-ending semantics)."""
    from dollos.tools import Report

    ctx, _, _ = _make_ctx(tmp_path)
    assert ctx.agent_report is None

    out = await Report(
        status="ok", summary="done", details="data here"
    ).run(ctx)

    assert out is None
    assert ctx.agent_report == {
        "status": "ok",
        "summary": "done",
        "details": "data here",
    }


# ----- WriteSchedule (Phase 1 schedule; add / remove, plan 2026-10-28 Task 1) -----


def _schedule_path(tmp_path: Path) -> Path:
    return tmp_path / "schedule" / f"{date.today():%Y-%m-%d}.toml"


def _seed_schedule(tmp_path: Path, *entries: tuple[str, str]) -> None:
    from datetime import time as _time

    from dollos.schedule import ScheduleEntry, write_schedule

    write_schedule(_schedule_path(tmp_path), [
        ScheduleEntry(time=_time.fromisoformat(t), intent=intent) for t, intent in entries
    ])


def _day(tmp_path: Path) -> list[tuple[str, str]]:
    from dollos.schedule import load_schedule

    sched = load_schedule(_schedule_path(tmp_path))
    assert sched is not None
    return [(f"{e.time:%H:%M:%S}", e.intent) for e in sched.entries]


@pytest.mark.asyncio
async def test_write_schedule_writes_toml_file(tmp_path):
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    out = await WriteSchedule(add=[
        ScheduleEntryArg(time="11:30:00", intent="lunch"),
        ScheduleEntryArg(time="07:30:00", intent="morning"),
    ]).run(ctx)
    assert "2 entries" in out
    # Written sorted by time.
    assert _day(tmp_path) == [("07:30:00", "morning"), ("11:30:00", "lunch")]


@pytest.mark.asyncio
async def test_write_schedule_add_keeps_existing_entries(tmp_path):
    """Live acceptance 2026-10-28 (3.5): adding a reminder wiped the day's two
    entries. add merges into today's file."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(add=[ScheduleEntryArg(time="15:00:00", intent="tea")]).run(ctx)
    assert _day(tmp_path) == [
        ("09:26:00", "stretch"), ("15:00:00", "tea"), ("20:00:00", "dinner"),
    ]


@pytest.mark.asyncio
async def test_write_schedule_add_at_existing_time_replaces_that_entry(tmp_path):
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(add=[ScheduleEntryArg(time="20:00", intent="late dinner")]).run(ctx)
    assert _day(tmp_path) == [("09:26:00", "stretch"), ("20:00:00", "late dinner")]


@pytest.mark.asyncio
async def test_write_schedule_remove_deletes_by_time(tmp_path):
    from dollos.tools import WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    out = await WriteSchedule(remove=["09:26:00"]).run(ctx)
    assert _day(tmp_path) == [("20:00:00", "dinner")]
    assert out == "Schedule now (1 entry): 20:00:00 — dinner"


@pytest.mark.asyncio
async def test_write_schedule_remove_then_add_reschedules(tmp_path):
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(
        remove=["20:00:00"], add=[ScheduleEntryArg(time="21:00:00", intent="dinner")],
    ).run(ctx)
    assert _day(tmp_path) == [("09:26:00", "stretch"), ("21:00:00", "dinner")]


@pytest.mark.asyncio
async def test_write_schedule_remove_of_missing_time_says_so_and_does_the_rest(tmp_path):
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    out = await WriteSchedule(
        remove=["10:00:00", "09:26:00"],
        add=[ScheduleEntryArg(time="15:00:00", intent="tea")],
    ).run(ctx)
    assert _day(tmp_path) == [("15:00:00", "tea"), ("20:00:00", "dinner")]
    assert out == (
        "No entry at 10:00:00, so nothing removed there. "
        "Schedule now (2 entries): 15:00:00 — tea; 20:00:00 — dinner"
    )


@pytest.mark.asyncio
async def test_write_schedule_result_lists_the_whole_day(tmp_path):
    """She sees her schedule in the result, past entries included."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("00:00:01", "past"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    out = await WriteSchedule(add=[ScheduleEntryArg(time="09:26:00", intent="stretch")]).run(ctx)
    assert out == (
        "Schedule now (3 entries): 00:00:01 — past; 09:26:00 — stretch; 20:00:00 — dinner"
    )


@pytest.mark.asyncio
async def test_write_schedule_removing_the_last_entry_leaves_an_empty_day(tmp_path):
    from dollos.tools import WriteSchedule

    _seed_schedule(tmp_path, ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    out = await WriteSchedule(remove=["20:00:00"]).run(ctx)
    assert out == "Schedule now (0 entries)"
    assert _day(tmp_path) == []


@pytest.mark.asyncio
async def test_write_schedule_with_nothing_to_change_is_an_error(tmp_path):
    from dollos.harness.executor import dispatch_one
    from dollos.tools import WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    r = await dispatch_one("WriteSchedule", {}, ctx, {"WriteSchedule": WriteSchedule})
    assert r is not None and not r.success
    assert "add" in r.detail and "remove" in r.detail
    r = await dispatch_one("WriteSchedule", {"add": [], "remove": []}, ctx,
                           {"WriteSchedule": WriteSchedule})
    assert r is not None and not r.success
    assert not _schedule_path(tmp_path).exists()
    assert not ctx.mind_state.recent_outputs


@pytest.mark.asyncio
async def test_write_schedule_on_a_file_it_cannot_read_names_it_and_changes_nothing(tmp_path):
    """A hand-edited file tomllib cannot read: writing over it would lose what
    the owner put there, so the call fails, saying which file and why."""
    from dollos.harness.executor import dispatch_one
    from dollos.tools import WriteSchedule

    path = _schedule_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('[[entry]]\ntime = 09:00\nintent = "dentist"\n')
    ctx, _ms, _sink = _make_ctx(tmp_path)
    r = await dispatch_one("WriteSchedule", {
        "add": [{"time": "15:00:00", "intent": "tea"}],
    }, ctx, {"WriteSchedule": WriteSchedule})
    assert r is not None and not r.success
    assert f"cannot read memory/schedule/{path.name}" in r.detail
    assert "line 2" in r.detail  # tomllib's own reason
    assert "nothing was changed" in r.detail
    assert path.read_text() == '[[entry]]\ntime = 09:00\nintent = "dentist"\n'
    assert not (tmp_path / "shared").exists()


def test_write_schedule_rejects_two_adds_at_one_time():
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    with pytest.raises(ValidationError, match="09:00:00"):
        WriteSchedule(add=[
            ScheduleEntryArg(time="09:00:00", intent="a"),
            ScheduleEntryArg(time="09:00", intent="b"),
        ])


def test_write_schedule_validates_remove_times():
    from dollos.tools import WriteSchedule

    with pytest.raises(ValidationError):
        WriteSchedule(remove=["not a time"])
    with pytest.raises(ValidationError):
        WriteSchedule(remove=["25:00:00"])


@pytest.mark.asyncio
async def test_write_schedule_writes_markdown_summary(tmp_path):
    """gap #3 — also writes a markdown summary into shared so Recall can find."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, _ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(add=[
        ScheduleEntryArg(time="07:30:00", intent="morning"),
    ]).run(ctx)

    today_str = f"{date.today():%Y-%m-%d}"
    md_path = tmp_path / "shared" / f"{today_str}.md"
    assert md_path.exists()
    body = md_path.read_text()
    assert re.search(r"^## .+ Plan$", body, re.M)
    assert "## " in body
    assert "07:30:00 — morning" in body


@pytest.mark.asyncio
async def test_write_schedule_markdown_has_only_this_calls_additions(tmp_path):
    """The Plan block lists what this call added — not the entries already
    there, and no block at all for a call that only removes."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    _seed_schedule(tmp_path, ("09:26:00", "stretch"), ("20:00:00", "dinner"))
    ctx, _ms, _sink = _make_ctx(tmp_path)
    md_path = tmp_path / "shared" / f"{date.today():%Y-%m-%d}.md"
    await WriteSchedule(add=[ScheduleEntryArg(time="15:00:00", intent="tea")]).run(ctx)
    body = md_path.read_text()
    assert "15:00:00 — tea" in body
    assert "stretch" not in body and "dinner" not in body

    await WriteSchedule(remove=["15:00:00"]).run(ctx)
    assert md_path.read_text() == body


@pytest.mark.asyncio
async def test_write_schedule_indexes_markdown_with_memsearch(tmp_path):
    """gap #3 — markdown summary is indexed in memsearch so Recall surfaces it."""
    from dollos.tools import ScheduleEntryArg, WriteSchedule

    ctx, ms, _sink = _make_ctx(tmp_path)
    await WriteSchedule(add=[
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


# ---------- ReadToolOutput ----------


@pytest.mark.asyncio
async def test_read_tool_output_returns_slice(tmp_path: Path) -> None:
    store = ToolOutputStore(tmp_path)
    output_id = store.write("\n".join(f"row {i}" for i in range(50)))
    ctx, _ms, _sink = _make_ctx(tmp_path)
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


@pytest.mark.asyncio
async def test_scratchpad_set_then_append_then_clear(tmp_path):
    from dollos.tools import Scratchpad
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await Scratchpad(op="set", content="line A").run(ctx)
    assert ctx.mind_state.scratchpad == "line A"
    await Scratchpad(op="append", content="line B").run(ctx)
    assert "line A" in ctx.mind_state.scratchpad
    assert "line B" in ctx.mind_state.scratchpad
    await Scratchpad(op="clear", content="").run(ctx)
    assert ctx.mind_state.scratchpad == ""


@pytest.mark.asyncio
async def test_scratchpad_append_over_cap_raises(tmp_path):
    import pytest as _pytest
    from dollos.tools import Scratchpad
    ctx, _ms, _sink = _make_ctx(tmp_path)
    await Scratchpad(op="set", content="x" * 1999).run(ctx)
    with _pytest.raises(ValueError):
        await Scratchpad(op="append", content="y" * 100).run(ctx)


# ---------- MoodTool ----------


@pytest.mark.asyncio
async def test_mood_tool_updates_mind_state(tmp_path):
    from dollos.tools import MoodTool

    state = MindState()
    ctx, _ms, _sink = _make_ctx(tmp_path, state=state)
    result = await MoodTool(emotion="happy", reason="因為今天天氣好").run(ctx)
    assert state.mood.emotion == "happy"
    assert state.mood.reason == "因為今天天氣好"
    assert "happy" in result


@pytest.mark.asyncio
async def test_mood_tool_no_reason(tmp_path):
    from dollos.tools import MoodTool

    state = MindState()
    ctx, _ms, _sink = _make_ctx(tmp_path, state=state)
    await MoodTool(emotion="calm").run(ctx)
    assert state.mood.emotion == "calm"
    assert state.mood.reason == ""


# ---------- OutputRecord side-effects ----------


@pytest.mark.asyncio
async def test_note_memory_appends_output_record(tmp_path):
    state = MindState()
    ctx, _ms, _sink = _make_ctx(tmp_path, state=state)
    await NoteMemory(text="主人喜歡咖啡").run(ctx)
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "NoteMemory"


@pytest.mark.asyncio
async def test_recall_appends_output_record(tmp_path):
    state = MindState()
    ctx = _ctx_with_search(tmp_path, hits=[{"content": "x", "source": "a.md"}])
    ctx.mind_state = state
    await Recall(query="test").run(ctx)
    assert len(state.recent_outputs) == 1
    assert state.recent_outputs[0].kind == "Recall"


# ---------- NoteToolLesson ----------


@pytest.mark.asyncio
async def test_note_tool_lesson_appends_and_indexes(tmp_path):
    from dollos.tools import NoteToolLesson
    ctx, ms, _sink = _make_ctx(tmp_path)
    out = await NoteToolLesson(situation="grepping large output",
                               lesson="use ReadToolOutput, not Shell cat").run(ctx)
    assert out.startswith("lesson noted:")
    path = tmp_path / "shared" / "tool_playbook.md"
    text = path.read_text()
    assert "[situation] grepping large output" in text
    assert "use ReadToolOutput" in text
    # timestamp-only heading (no [k:v] axis tags that would leak into associative)
    import re
    heading = next(l for l in text.splitlines() if l.startswith("## "))
    assert re.match(r"## \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", heading)
    # append-only: a second lesson adds, never overwrites
    await NoteToolLesson(situation="s2", lesson="l2").run(ctx)
    assert "[situation] grepping large output" in path.read_text()
    assert "[situation] s2" in path.read_text()


# ---------- MoodTool: one English word ----------


@pytest.mark.parametrize("emotion", ["happy", "uneasy", "Calm", "a", "x" * 12])
def test_mood_tool_accepts_one_english_word(emotion):
    from dollos.tools import MoodTool

    assert MoodTool(emotion=emotion).emotion == emotion


@pytest.mark.parametrize("emotion", [
    "開心",            # not English
    "a bit worried",   # a phrase, not one word
    "happy!",          # punctuation
    "well-rested",     # punctuation inside
    " happy",          # whitespace
    "happy1",          # digit
    "café",            # non-ASCII letter
    "",                # empty
    "x" * 13,          # longer than 12 letters
])
def test_mood_tool_rejects_anything_but_one_english_word(emotion):
    from dollos.tools import MoodTool

    with pytest.raises(ValidationError):
        MoodTool(emotion=emotion)


@pytest.mark.asyncio
async def test_mood_tool_rejection_reaches_her_as_a_clear_tool_result(tmp_path):
    """Through dispatch_one (the live path) the rejection is a failed tool
    result that says what an emotion has to be; her mood stays as it was."""
    from dollos.harness.executor import dispatch_one
    from dollos.tools import MoodTool

    state = MindState(mood=Mood(emotion="calm"))
    ctx, _ms, _sink = _make_ctx(tmp_path, state=state)
    r = await dispatch_one("MoodTool", {"emotion": "a bit worried"}, ctx,
                           {"MoodTool": MoodTool})
    assert r is not None and r.success is False
    assert "one English word" in r.detail
    assert "a bit worried" in r.detail
    assert state.mood.emotion == "calm"


# ---------- tool consolidation (plan 2026-10-29 decisions 2, 4) ----------


def test_doll_and_workers_have_no_grep_tool_output_or_scratchpad():
    from dollos.tools import KEEPER_TOOLS, MAIN_TOOLS, SUB_TOOLS
    for tools in (MAIN_TOOLS, SUB_TOOLS):
        names = {t.__name__ for t in tools}
        assert "GrepToolOutput" not in names
        assert "Scratchpad" not in names
        assert "ReadToolOutput" in names
    assert "Scratchpad" in {t.__name__ for t in KEEPER_TOOLS}


def test_monitor_tools_removed():
    """Plan 2026-10-29 decision 3: the monitor feature is gone."""
    import dollos.tools as tools_mod

    names = {t.__name__ for t in tools_mod.MAIN_TOOLS} | {t.__name__ for t in tools_mod.SUB_TOOLS}
    assert not names & {"SpawnMonitor", "RemoveMonitor"}
    assert not hasattr(tools_mod, "SpawnMonitor")
    assert not hasattr(tools_mod, "RemoveMonitor")
