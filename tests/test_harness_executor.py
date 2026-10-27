"""ToolExecutor (plan 2026-10-27 Task 4, S5): the three paths of one call
(allow / deny / unbound) and its one log record per call (spec §6.4)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from dollos.agent_engine import run_agent
from dollos.harness.executor import (
    ToolExecutor,
    format_unknown_tool,
    summarize_arguments,
    turn_label,
)
from dollos.llm.model import SpeechDelta, ToolCall, ToolCalls
from dollos.logging_config import configure_cascade_logging
from dollos.mind.mind_state import Perception
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import Recall, SetFocus, Shell
from tests._dispatcher_helpers import _FakeMemSearch, _make_mind_ctx
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop


def _records(log_root: Path) -> list[dict]:
    for h in logging.getLogger("cascade").handlers:
        h.flush()
    rows = []
    for f in log_root.glob("*.jsonl"):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "tool_call"]


def _executor(tmp_path, *, offered, bound, kind="conversation", principal="owner",
              dispatch=None):
    return ToolExecutor(
        _make_mind_ctx(tmp_path), offered={c.__name__: c for c in offered},
        bound={c.__name__: c for c in bound}, principal=principal, kind=kind,
        dispatch=dispatch,
    )


class _FakeShellRunner:
    def __init__(self):
        self.spawned: list[str] = []

    def spawn(self, *, command, timeout_s, response_sink):
        self.spawned.append(command)


# ---------------------------------------------------------------------------
# The three paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_offered_is_an_unknown_tool_failure(tmp_path):
    ex = _executor(tmp_path, offered=[Recall, SetFocus], bound=[SetFocus])
    assert ex.decide("Nope") == "deny"
    r = await ex.run("Nope", {})
    assert r.success is False and r.unbound is False
    assert r.detail == format_unknown_tool("Nope", {"SetFocus": SetFocus})


@pytest.mark.asyncio
async def test_offered_but_unbound_is_refused_without_running(tmp_path):
    runner = _FakeShellRunner()
    ex = _executor(tmp_path, offered=[Recall, Shell], bound=[Recall], kind="diary",
                   principal="internal")
    ex._ctx.shell_runner = runner
    assert ex.decide("Shell") == "unbound"
    r = await ex.run("Shell", {"command": "echo hi"})
    assert r.unbound is True and r.status == "rejected_unbound"
    assert r.detail == "Shell is not available in this turn (diary); available now: Recall"
    assert runner.spawned == []


@pytest.mark.asyncio
async def test_bound_validates_then_runs(tmp_path):
    ex = _executor(tmp_path, offered=[SetFocus], bound=[SetFocus])
    assert ex.decide("SetFocus") == "allow"
    bad = await ex.run("SetFocus", {})
    assert bad.success is False and "text" in bad.detail
    await ex.run("SetFocus", {"text": "breakfast"})
    assert ex._ctx.mind_state.focus == "breakfast"


@pytest.mark.asyncio
async def test_dispatch_hook_gets_allow_and_deny_not_unbound(tmp_path):
    seen: list[tuple[str, list[str]]] = []

    async def _dispatch(name, arguments, bound):
        seen.append((name, sorted(bound)))
        return None

    ex = _executor(tmp_path, offered=[Recall, SetFocus], bound=[SetFocus],
                   dispatch=_dispatch)
    await ex.run("SetFocus", {"text": "x"})
    await ex.run("Nope", {})
    await ex.run("Recall", {"query": "x"})
    assert seen == [("SetFocus", ["SetFocus"]), ("Nope", ["SetFocus"])]


@pytest.mark.parametrize(("kind", "principal", "label"), [
    ("safe_mode", "external_public", "safe mode"),
    ("safe_mode", "owner", "safe mode"),
    ("reflection", "external_public", "external_public"),
    ("conversation", "owner", "owner"),
    ("conversation", "owner_dm", "owner"),
    ("conversation", "internal", "normal"),
    ("diary", "internal", "diary"),
])
def test_turn_label(kind, principal, label):
    assert turn_label(kind, principal) == label


# ---------------------------------------------------------------------------
# One log record per call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_record_per_call_with_its_decision(tmp_path):
    configure_cascade_logging(tmp_path / "log")
    ex = _executor(tmp_path, offered=[Recall, SetFocus], bound=[SetFocus],
                   kind="pulse", principal="internal")
    await ex.run("SetFocus", {"text": "x"})
    await ex.run("Nope", {"a": 1})
    await ex.run("Recall", {"query": "q"})
    rows = _records(tmp_path / "log")
    assert [
        {k: r[k] for k in ("principal", "kind", "tool", "args", "decision")} for r in rows
    ] == [
        {"principal": "internal", "kind": "pulse", "tool": "SetFocus",
         "args": '{"text": "x"}', "decision": "allow"},
        {"principal": "internal", "kind": "pulse", "tool": "Nope",
         "args": '{"a": 1}', "decision": "deny"},
        {"principal": "internal", "kind": "pulse", "tool": "Recall",
         "args": '{"query": "q"}', "decision": "unbound"},
    ]


def test_shell_command_is_masked_after_40_chars():
    command = "curl -H 'Authorization: Bearer " + "s3cr3t" * 10 + "' https://x"
    summary = summarize_arguments("Shell", {"command": command, "timeout_s": 5})
    assert command[:40] in summary
    assert command[40:] not in summary and "s3cr3ts3cr3t" not in summary
    assert f"…(+{len(command) - 40} chars)" in summary
    assert '"timeout_s": 5' in summary
    assert summarize_arguments("Shell", {"command": "ls"}) == '{"command": "ls"}'


def test_other_arguments_are_cut():
    summary = summarize_arguments("NoteMemory", {"text": "x" * 1000})
    assert len(summary) == 201 and summary.endswith("…")


@pytest.mark.asyncio
async def test_shell_record_never_holds_the_whole_command(tmp_path):
    configure_cascade_logging(tmp_path / "log")
    command = "echo " + "token-" * 20
    ex = _executor(tmp_path, offered=[Shell], bound=[], kind="agenda",
                   principal="internal")
    await ex.run("Shell", {"command": command})
    [row] = _records(tmp_path / "log")
    assert row["decision"] == "unbound"
    assert command not in row["args"] and command[:40] in row["args"]


# ---------------------------------------------------------------------------
# Both loops go through the executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_writes_records(tmp_path):
    configure_cascade_logging(tmp_path / "log")
    model = FakeModel([
        '<tool_call>{"name": "Report", "arguments": '
        '{"status": "ok", "summary": "s", "details": "d"}}</tool_call>'
    ])
    await run_agent(
        task="t", system="SYS", model=model, renderer=PromptRenderer(),
        memory_root=tmp_path / "memory", memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        kind="keeper", think_budget=64, purpose="consolidation",
    )
    [row] = _records(tmp_path / "log")
    assert (row["principal"], row["kind"], row["tool"], row["decision"]) == (
        "internal", "keeper", "Report", "allow",
    )


@pytest.mark.asyncio
async def test_mindloop_run_writes_records(tmp_path):
    configure_cascade_logging(tmp_path / "log")
    model = FakeModel([
        [SpeechDelta("ok. "), ToolCalls((
            ToolCall(id="a", name="SetFocus", arguments='{"text": "tea"}'),
            ToolCall(id="b", name="Nope", arguments="{}"),
        ))],
        [SpeechDelta("done")],
    ])
    ml = make_mindloop(memory_root=tmp_path / "memory", llm=model)
    await ml._run_one_turn([Perception(kind="UserSpoke", t=time.time(), data={"text": "hi"})])
    rows = _records(tmp_path / "log")
    assert [(r["principal"], r["kind"], r["tool"], r["decision"]) for r in rows] == [
        ("owner", "conversation", "SetFocus", "allow"),
        ("owner", "conversation", "Nope", "deny"),
    ]


@pytest.mark.asyncio
async def test_mindloop_unbound_writes_a_record_but_no_tool_memory(tmp_path):
    configure_cascade_logging(tmp_path / "log")
    ml = make_mindloop(memory_root=tmp_path / "memory")
    ml._ctx.principal = "internal"
    ml._moment = "diary"
    r = await ml._dispatch_bound("Shell", {"command": "ls"}, ml._offered_tools())
    assert r.unbound is True
    assert ml._ctx.mind_state.tool_stats == {}
    [row] = _records(tmp_path / "log")
    assert (row["principal"], row["kind"], row["tool"], row["decision"]) == (
        "internal", "diary", "Shell", "unbound",
    )
