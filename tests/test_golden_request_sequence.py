"""Golden request sequences (harness plan 2026-10-27, Task 5).

The Turn Runner refactor must send the model exactly the same requests as
the two loops it replaced. Each test drives a real path with ``FakeModel``
(``tests/_fake_model.py``) and compares every request it recorded — the
messages, the tool JSON, and every other argument the adapter got — to a
pretty-JSON file under ``tests/golden/``:

- ``requests_owner_mood_then_speak.json``: an owner turn (``UserSpoke``)
  where the model calls MoodTool, then speaks, then ends.
- ``requests_owner_interrupted.json``: an owner turn cut mid-speech (the
  cascade is cancelled after its first sentence), then the next owner turn,
  whose request carries what the cut reply actually said.
- ``requests_worker_recall_report.json``: a workflow worker
  (``WorkflowRunner._spawn_agent``) that calls Recall, then Report.
- ``requests_keeper_report.json``: an evolution keeper
  (``evolution_keeper._run_keeper_agent``) that calls Report.

A MindLoop request carries the wall clock ([Now], perception times), so the
owner tests freeze ``time.time`` and ``datetime.now`` in the modules that
render them. Nothing else is normalized; the tmp path is asserted absent.

These were recorded at ``c1cbcea`` (before the refactor). A deliberate change
updates them with ``DOLLOS_UPDATE_GOLDEN=1``; review the diff before committing.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dollos.llm.model import ModelEvent, SpeechDelta
from dollos.mind.mind_state import Perception
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE = os.environ.get("DOLLOS_UPDATE_GOLDEN") == "1"


def _call(name: str, **arguments) -> str:
    return "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>"


_REPORT = _call("Report", status="ok", summary="s", details="d")


def _check(name: str, calls: list[dict], tmp_path: Path) -> None:
    text = json.dumps(calls, ensure_ascii=False, indent=2) + "\n"
    assert str(tmp_path) not in text
    path = GOLDEN_DIR / name
    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.read_text(encoding="utf-8") == text, (
        f"{name} changed; if deliberate, rerun with DOLLOS_UPDATE_GOLDEN=1"
    )


class _FakeMemSearch:
    async def index_file(self, path):
        return None

    async def search(self, query: str, top_k: int = 5, **kw):
        return []


@pytest.fixture
def frozen_clock(monkeypatch):
    """A fixed wall clock for everything a MindLoop request renders."""
    import datetime as _dt
    import time as _time

    t0 = 1_793_000_000.0  # 2026-10-26, a fixed instant

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.fromtimestamp(t0, tz)

    monkeypatch.setattr(_time, "time", lambda: t0)
    monkeypatch.setattr(_dt, "datetime", _FrozenDateTime)
    return t0


def _owner(text: str, t: float) -> Perception:
    return Perception(kind="UserSpoke", t=t, data={"text": text})


@pytest.mark.asyncio
async def test_owner_turn_mood_then_speak(tmp_path, frozen_clock):
    model = FakeModel([
        "she is pleased</think>" + _call("MoodTool", emotion="happy", intensity=0.6),
        "glad you said that</think>Hi there. Nice to see you.",
    ], usages=[(900, 40), (960, 25)])
    ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
    await ml._run_one_turn([_owner("hello", frozen_clock)])
    _check("requests_owner_mood_then_speak.json", model.calls, tmp_path)


class _CancelAfter(FakeModel):
    """Cancels the MindLoop's cascade once ``after`` events of the first call
    were yielded (an interrupt arriving mid-speech)."""

    def __init__(self, replies, *, after: int) -> None:
        super().__init__(replies)
        self.loop = None
        self._after = after

    async def stream(self, **kw) -> AsyncIterator[ModelEvent]:
        first = len(self.calls) == 0
        n = 0
        async for ev in super().stream(**kw):
            yield ev
            n += 1
            if first and n == self._after:
                self.loop.cancel_current_cascade()


@pytest.mark.asyncio
async def test_owner_turn_interrupted_mid_speech(tmp_path, frozen_clock):
    model = _CancelAfter([
        [SpeechDelta("First sentence. "), SpeechDelta("Second sen"),
         SpeechDelta("tence never said.")],
        "</think>Okay.",
    ], after=2)
    model._usages = [(800, 30), (870, 10)]
    ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
    model.loop = ml
    await ml._run_one_turn([_owner("tell me a story", frozen_clock)])
    await ml._run_one_turn([_owner("go on", frozen_clock)])
    _check("requests_owner_interrupted.json", model.calls, tmp_path)


@pytest.mark.asyncio
async def test_workflow_worker_recall_then_report(tmp_path):
    from dollos.workflow import WorkflowRunner

    model = FakeModel([
        "look it up</think>" + _call("Recall", query="the plan"),
        "found it</think>" + _REPORT,
    ], usages=[(300, 20), (350, 15)])
    runner = WorkflowRunner(
        model=model,
        renderer=PromptRenderer(),
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
    )
    report = await runner._spawn_agent("find the plan")
    assert report == {"status": "ok", "summary": "s", "details": "d"}
    _check("requests_worker_recall_report.json", model.calls, tmp_path)


@pytest.mark.asyncio
async def test_keeper_report(tmp_path):
    from dollos.mind.evolution_keeper import _run_keeper_agent

    model = FakeModel(["weighing it</think>" + _REPORT], usages=[(400, 30)])
    report = await _run_keeper_agent(
        task="review the evidence",
        model=model,
        renderer=PromptRenderer(),
        memsearch=_FakeMemSearch(),
        memory_root=tmp_path / "memory",
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        max_tokens=2048,
        think_budget_tokens=512,
    )
    assert report == {"status": "ok", "summary": "s", "details": "d"}
    _check("requests_keeper_report.json", model.calls, tmp_path)
