"""Golden snapshots of what the model is shown (harness plan 2026-10-27, Task 0).

The tool JSON sits at the very front of every request, so the harness
refactor (Tasks 2–5) must leave each tier's list byte-for-byte unchanged
(plan decision 4); otherwise the server's cache prefix is recomputed. Each
test drives the real code path with ``FakeModel`` (``tests/_fake_model.py``),
takes the first request it records, and compares ``{"system", "tools"}`` to a
pretty-JSON file under ``tests/golden/``:

- ``offered_tools_internal.json`` / ``_external_dm.json`` /
  ``_external_public.json``: one ``MindLoop._run_one_turn`` per origin tier
  (``make_mindloop`` with ``self_profile_enabled`` and ``evolution_enabled``
  on, so PinSelf and SelfRevision are offered on ``internal``). The tier comes
  from the perception: ``UserSpoke`` → internal; owner ``ChannelMessage`` in a
  DM → external_dm; non-owner ``ChannelMessage`` → external_public.
- ``agent_tools_worker.json`` / ``agent_tools_keeper.json``: one ``run_agent``
  with ``SUB_TOOLS`` (workflow worker) and ``KEEPER_TOOLS`` (consolidation /
  evolution keeper), system = ``PromptRenderer().render("subagent_scaffolding")``
  as ``WorkflowRunner`` and the keepers render it.

``system`` is the content of the request's first (system) message, ``tools``
the ``tools=`` list the ModelAdapter received. Nothing is normalized: neither
contains a timestamp, tmp path or id (the tests assert the tmp path is absent).

A deliberate change (e.g. plan Task 1 adds a field to Recall) updates every
golden file in one command; review the diff before committing:

    DOLLOS_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_offered_tools.py
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dollos.agent_engine import run_agent
from dollos.mind.mind_state import Perception
from dollos.prompts import PromptRenderer
from dollos.tool_outputs import ToolOutputStore
from dollos.tools import KEEPER_TOOLS, SUB_TOOLS
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE = os.environ.get("DOLLOS_UPDATE_GOLDEN") == "1"

_REPORT = (
    '<tool_call>{"name": "Report", "arguments": '
    '{"status": "ok", "summary": "s", "details": "d"}}</tool_call>'
)


def _p(kind: str, **data) -> Perception:
    return Perception(kind=kind, t=time.time(), data=data)


def _snapshot(call: dict, tmp_path: Path) -> str:
    assert call["messages"][0]["role"] == "system"
    text = json.dumps(
        {"system": call["messages"][0]["content"], "tools": call["tools"]},
        ensure_ascii=False, indent=2,
    ) + "\n"
    assert str(tmp_path) not in text
    return text


def _check(name: str, text: str) -> None:
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

    async def search(self, query: str, top_k: int = 5):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tier", "perception"), [
    ("internal", _p("UserSpoke", text="hello")),
    ("external_dm", _p("ChannelMessage", channel_id="dm-1", text="hello",
                       author_is_owner=True, is_dm=True)),
    ("external_public", _p("ChannelMessage", channel_id="pub-1", text="hello",
                           author_is_owner=False, is_dm=False)),
])
async def test_offered_tools_and_system_per_tier(tmp_path, tier, perception):
    model = FakeModel()
    ml = make_mindloop(
        memory_root=tmp_path, llm=model, system_prompt="You are Doll.",
        self_profile_enabled=True, evolution_enabled=True,
    )
    await ml._run_one_turn([perception])

    assert ml._ctx.origin_tier == tier
    _check(f"offered_tools_{tier}.json", _snapshot(model.calls[0], tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "tools"), [
    ("worker", SUB_TOOLS),
    ("keeper", KEEPER_TOOLS),
])
async def test_agent_tools_and_system(tmp_path, role, tools):
    model = FakeModel([_REPORT])
    renderer = PromptRenderer()
    report = await run_agent(
        task="do the task",
        system=renderer.render("subagent_scaffolding"),
        model=model,
        renderer=renderer,
        memory_root=tmp_path / "memory",
        memsearch=_FakeMemSearch(),
        transcripts_root=tmp_path / "transcripts",
        tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
        tools=tools,
        think_budget=1024,
        purpose=role,
    )

    assert report is not None
    _check(f"agent_tools_{role}.json", _snapshot(model.calls[0], tmp_path))
