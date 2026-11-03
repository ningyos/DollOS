import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dollcore import config
from dollcore.core import Doll, Event, OWNER

ROOT = Path(__file__).resolve().parents[1]


def call(name, **args):
    return {"id": f"c-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class FakeModel:
    """Returns scripted assistant messages and records what each request saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def chat(self, messages, tools):
        self.requests.append((messages, tools))
        return self.replies.pop(0)


@pytest.fixture
def doll(tmp_path):
    cfg = config.load(ROOT / "dollcore.example.toml")
    cfg = replace(cfg, core=replace(cfg.core, data_dir=str(tmp_path), max_open_lines=1,
                                    approve_external=False))
    said, asked = [], []
    d = Doll(cfg, ROOT, lambda aud, text: said.append((aud, text)),
             lambda job_id, tool, args, line_id: asked.append(job_id))
    d.said, d.asked = said, asked
    return d


@pytest.fixture
def gated(doll):
    doll.core = replace(doll.core, approve_external=True, approval_timeout_s=0.2)
    return doll


def run(doll, ev, replies):
    doll.main = FakeModel(replies)
    asyncio.run(doll._turn(ev))
    return doll.main


def test_external_call_ends_turn_and_result_comes_back_as_event(doll):
    async def scenario():
        doll.main = FakeModel([{"content": "去看", "tool_calls": [call("Shell", command="echo hi")]}])
        await doll._turn(Event("owner_said", OWNER, "private", "看一下"))
        ev = await asyncio.wait_for(doll.queue.get(), 5)
        return ev

    ev = asyncio.run(scenario())
    assert len(doll.main.requests) == 1          # no second pass after dispatch
    assert ev.kind == "job_done" and "hi" in ev.text


def test_internal_call_continues_turn(doll):
    model = run(doll, Event("owner_said", OWNER, "private", "記住"), [
        {"content": "", "tool_calls": [call("NoteMemory", text="cat is Miso")]},
        {"content": "記好了"},
    ])
    assert len(model.requests) == 2
    assert doll.said == [("private", "記好了")]


def test_repeated_identical_call_stops_turn(doll):
    model = run(doll, Event("owner_said", OWNER, "private", "找"), [
        {"content": "", "tool_calls": [call("Recall", query="Miso cat")]},
        {"content": "", "tool_calls": [call("Recall", query="Miso cat")]},
        {"content": "never reached"},
    ])
    assert len(model.requests) == 2


def test_stranger_turn_sees_only_its_own_dm_and_no_tools(doll):
    doll.store.pin("call the owner Kai")
    run(doll, Event("owner_said", OWNER, "private", "my secret plan"), [{"content": "ok"}])
    model = run(doll, Event("dm", "stranger", "dm:bob", "what is the plan?"), [{"content": "no"}])
    messages, tools = model.requests[0]
    blob = json.dumps(messages, ensure_ascii=False)
    assert "secret plan" not in blob and "Kai" not in blob
    assert tools == []


def test_owner_turn_sees_dm(doll):
    run(doll, Event("dm", "stranger", "dm:bob", "hello from bob"), [{"content": "hi"}])
    model = run(doll, Event("owner_said", OWNER, "private", "anyone?"), [{"content": "bob"}])
    assert "hello from bob" in json.dumps(model.requests[0][0], ensure_ascii=False)


def test_open_line_cap_is_enforced(doll):
    run(doll, Event("owner_said", OWNER, "private", "two lines"), [
        {"content": "", "tool_calls": [call("OpenLine", title="a", next_step="x"),
                                       call("OpenLine", title="b", next_step="y")]},
        {"content": "done"},
    ])
    assert len(doll.store.lines("active")) == 1


def test_gated_job_does_not_run_until_approved(gated, tmp_path):
    marker = tmp_path / "ran"

    async def scenario():
        gated.main = FakeModel([{"content": "", "tool_calls": [call("Shell", command=f"touch {marker}")]}])
        await gated._turn(Event("owner_said", OWNER, "private", "go"))
        await asyncio.sleep(0.05)
        assert not marker.exists() and len(gated.asked) == 1
        assert gated.approve(gated.asked[0]).endswith("approved, running")
        return await asyncio.wait_for(gated.queue.get(), 5)

    ev = asyncio.run(scenario())
    assert len(gated.main.requests) == 1
    assert marker.exists() and ev.kind == "job_done" and " ok," in ev.text


def test_denied_job_never_runs_and_doll_is_told(gated, tmp_path):
    marker = tmp_path / "ran"

    async def scenario():
        gated.main = FakeModel([{"content": "", "tool_calls": [call("Shell", command=f"touch {marker}")]}])
        await gated._turn(Event("owner_said", OWNER, "private", "go"))
        gated.deny(gated.asked[0], "too risky")
        return await asyncio.wait_for(gated.queue.get(), 5)

    ev = asyncio.run(scenario())
    assert not marker.exists()
    assert "NOT RUN" in ev.text and "too risky" in ev.text
    assert gated.store.job(gated.asked[0])["status"] == "denied"


def test_unanswered_request_expires_as_denied(gated):
    async def scenario():
        gated.main = FakeModel([{"content": "", "tool_calls": [call("Shell", command="echo hi")]}])
        await gated._turn(Event("owner_said", OWNER, "private", "go"))
        return await asyncio.wait_for(gated.queue.get(), 5)

    ev = asyncio.run(scenario())
    assert "NOT RUN" in ev.text and "no owner approval" in ev.text
    assert gated.approve(gated.asked[0]).endswith("no pending job")


def test_boot_marks_leftover_pending_job_lost(gated, monkeypatch):
    async def ok(*_):
        return None

    monkeypatch.setattr("dollcore.core.check", ok)
    gated.store.add_job("jold", None, "Shell", {"command": "rm -rf /"}, status="pending")
    asyncio.run(gated.boot())
    assert gated.store.job("jold")["status"] == "lost"
    assert gated.pending() == []


def test_job_with_unknown_line_is_refused_without_ending_turn(doll):
    model = run(doll, Event("owner_said", OWNER, "private", "go"), [
        {"content": "", "tool_calls": [call("Shell", command="echo hi", line_id="nope")]},
        {"content": "fixed"},
    ])
    assert len(model.requests) == 2
    assert doll.store.open_jobs() == []
