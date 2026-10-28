"""Taint follows the page; tainted notes go to ``external_owner/`` (plan
2026-10-28 decision 4, harness spec §6.5).

Live 3.8 (``docs/research/2026-10-28-v1-tool-use-acceptance.md``): she read a
web page through an ``untrusted_output`` MCP tool, the turn was tainted and
NoteMemory refused; on the follow-up turn the taint was gone and she wrote
the page's points into ``shared/`` as plain notes. Now, driven through
``MindLoop._run_one_turn`` with ``FakeModel`` scripting the model and the
stdio fake MCP server as the untrusted "web" tool:

- a tainted turn's NoteMemory writes ``external_owner/<date>.md``, never
  ``shared/``, and says it stored external content; its ``[Recent outputs]``
  record and action-log line leave the note's text out;
- the next turn on the same page is still tainted (``Conversation.tainted``):
  Shell is refused with "this conversation has external content in it", and
  NoteMemory still goes to ``external_owner/``;
- a turn that raised after reading still leaves its page tainted;
- a new page (here: the budget) starts untainted, and its notes go to
  ``shared/`` again; the diary's one-off request never inherits the taint,
  and does not clear the page's either;
- an external_public turn on a tainted page is not tainted (its notes go to
  ``external_public/``) and leaves the page tainted;
- ``external_owner/`` never reaches an owner turn's auto-injected memory
  (``[Memory context]``, ``[Associative memories]``); ``Recall`` reads it only
  with ``include_external`` (which taints), tagged as external content; a
  stranger's Recall never reads it;
- the boot reindex (``kernel.build_memsearch``) covers ``external_owner/``.

Real ``FtsMemory`` over tmp dirs where memory scoping is asserted.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dollos.harness.executor import ToolExecutor
from dollos.llm.model import SpeechDelta
from dollos.mind.context_builder import PRIVATE
from dollos.mind.mind_prompt import _render_associative, _render_memory
from dollos.mind.mind_state import Perception
from dollos.tools import NoteMemory, Recall, Shell, _format_hit
from tests._dispatcher_helpers import _make_mind_ctx
from tests._fake_model import FakeModel
from tests._mindloop_factory import make_mindloop
from tests.test_harness_mcp_client import _boot, _server
from tests.test_harness_permissions import (
    _calls,
    _memsearch,
    _owner,
    _owner_dm,
    _records,
    _stranger,
    _tool_messages,
)
from tests.test_kernel_factories import _make_settings

_READ = "is not available in this turn (after reading external content)"
_PAGE = "is not available in this turn (this conversation has external content in it)"
_STORED = "memory noted as external content (Recall finds it only with include_external)"
_EXTERNAL_READ = "[external content you read · unverified]"

PAGE_TEXT = "Project Lighthouse: launch moved to November 14 unique-lh-818"
NOTE = "Lighthouse launch moved to November 14 unique-lh-818"
_FETCH = ("web__echo", {"text": PAGE_TEXT})
_SHELL = ("Shell", {"command": "ls"})
_NOTE = ("NoteMemory", {"text": NOTE})


@pytest.fixture
async def web():
    """The untrusted "web" tool: the fake server's echo, ``untrusted_output``."""
    clients, classes = await _boot(_server("web", tools=["echo"], untrusted_output=True))
    yield classes
    await clients.stop()


def _loop(tmp_path: Path, model: FakeModel, web, ms=None, **kw):
    ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.", **kw)
    ml.register_mcp_tools(web)
    if ms is not None:
        ml._ctx.memsearch = ms
    return ml


def _today(tmp_path: Path, subdir: str) -> Path:
    return tmp_path / subdir / f"{date.today():%Y-%m-%d}.md"


def _notes(tmp_path: Path, subdir: str) -> str:
    path = _today(tmp_path, subdir)
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# The reading turn
# ---------------------------------------------------------------------------


async def test_note_on_the_reading_turn_goes_to_external_owner(tmp_path, web):
    model = FakeModel([
        _calls(_FETCH),
        _calls(_NOTE),
        [SpeechDelta("Saved it, marked as from the page.")],
    ])
    ml = _loop(tmp_path, model, web)
    await ml._run_one_turn([_owner("read the notes page and save the points")])

    assert ml._ctx.tainted is True
    page, note = _tool_messages(model)
    assert page == PAGE_TEXT
    assert note == f"{_STORED}: {NOTE[:60]}"
    assert NOTE in _notes(tmp_path, "external_owner")
    assert not list((tmp_path / "shared").glob("*.md"))
    # The note's text stays out of what a later (untainted) page or the
    # diary reads back: [Recent outputs] and the owner-tier action log.
    assert [o.summary for o in ml._state.recent_outputs if o.kind == "NoteMemory"] == [
        "noted external content",
    ]
    assert not any("unique-lh-818" in o.summary for o in ml._state.recent_outputs)
    log = _notes(tmp_path, "transcripts")
    assert "noted external content" in log
    assert "unique-lh-818" not in log


# ---------------------------------------------------------------------------
# The page keeps the taint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("perception", [_owner, _owner_dm])
async def test_next_turn_on_the_same_page_is_still_tainted(tmp_path, web, perception):
    model = FakeModel([
        _calls(_FETCH), [SpeechDelta("It says the launch moved.")],
        _calls(_SHELL, _NOTE), [SpeechDelta("ok")],
    ])
    ml = _loop(tmp_path, model, web)
    await ml._run_one_turn([perception("what does the page say?")])
    await ml._run_one_turn([perception("did you save them?")])

    # Same page: the fetched text is still in the conversation.
    assert model.calls[2]["conversation"] == model.calls[0]["conversation"]
    assert ml._ctx.tainted is True
    assert ml._ctx.shell_runner.calls == []
    shell, note = _tool_messages(model)[-2:]
    assert shell == f"Shell {_PAGE}; available now: NoteMemory, Recall"
    assert note == f"{_STORED}: {NOTE[:60]}"
    assert NOTE in _notes(tmp_path, "external_owner")
    assert not list((tmp_path / "shared").glob("*.md"))


async def test_page_refusal_is_logged_tainted(tmp_path, web):
    from dollos.logging_config import configure_cascade_logging

    configure_cascade_logging(tmp_path / "log")
    model = FakeModel([
        _calls(_FETCH), [SpeechDelta("read it")],
        _calls(_SHELL), [SpeechDelta("ok")],
    ])
    ml = _loop(tmp_path, model, web)
    await ml._run_one_turn([_owner()])
    await ml._run_one_turn([_owner()])

    rows = _records(tmp_path / "log", "tool_call")
    assert [(r["tool"], r["decision"], r["tainted"]) for r in rows] == [
        ("web__echo", "allow", False),
        ("Shell", "unbound", True),
    ]
    # Only the call that read the page taints; the next turn inherits it.
    [tainted] = _records(tmp_path / "log", "turn_tainted")
    assert tainted["tool"] == "web__echo"


async def test_a_turn_that_raised_after_reading_leaves_the_page_tainted(tmp_path, web):
    model = FakeModel([
        _calls(_FETCH),
        RuntimeError("stream broke"),
        _calls(_SHELL), [SpeechDelta("ok")],
    ])
    ml = _loop(tmp_path, model, web)
    with pytest.raises(RuntimeError, match="stream broke"):
        await ml._run_one_turn([_owner()])
    await ml._run_one_turn([_owner("try again")])

    assert model.calls[2]["conversation"] == model.calls[0]["conversation"]
    assert ml._ctx.shell_runner.calls == []
    assert _tool_messages(model)[-1] == f"Shell {_PAGE}; available now: NoteMemory, Recall"


# ---------------------------------------------------------------------------
# A new page starts clean
# ---------------------------------------------------------------------------


async def test_a_new_page_starts_untainted(tmp_path, web):
    model = FakeModel(
        [
            _calls(_FETCH), [SpeechDelta("read it")],
            _calls(_SHELL, _NOTE), [SpeechDelta("ok")],
        ],
        usages=[(900, 50), (990, 20), (300, 10)],
    )
    ml = _loop(tmp_path, model, web, conversation_budget_tokens=1000)
    await ml._run_one_turn([_owner()])
    await ml._run_one_turn([_owner()])      # 1010 >= 1000: new page

    assert [c["conversation"] for c in model.calls] == [
        (PRIVATE, 1), (PRIVATE, 1), (PRIVATE, 2), (PRIVATE, 2),
    ]
    assert ml._ctx.tainted is False
    assert [c["command"] for c in ml._ctx.shell_runner.calls] == ["ls"]
    assert NOTE in _notes(tmp_path, "shared")
    assert _notes(tmp_path, "external_owner") == ""


async def test_the_diary_one_off_is_untainted_and_leaves_the_page_tainted(tmp_path, web):
    diary = ("WriteDiary", {"content": "A quiet day."})
    model = FakeModel([
        _calls(_FETCH), [SpeechDelta("read it")],
        _calls(diary), [],
        _calls(_SHELL), [SpeechDelta("ok")],
    ])
    ml = _loop(tmp_path, model, web)
    await ml._run_one_turn([_owner()])
    await ml._run_one_turn([Perception(kind="DiaryMoment", t=1.0, data={})])

    assert model.calls[2]["conversation"] is None   # the one-off request
    assert _tool_messages(model)[-1] == "diary written"
    assert "A quiet day." in _notes(tmp_path, "shared")

    await ml._run_one_turn([_owner()])
    assert model.calls[4]["conversation"] == model.calls[0]["conversation"]
    assert ml._ctx.shell_runner.calls == []
    assert _tool_messages(model)[-1] == f"Shell {_PAGE}; available now: NoteMemory, Recall"


async def test_external_public_turn_is_never_tainted_by_its_page(tmp_path):
    """No owner turn shares a page with a stranger's (the offered list
    differs, so the fixed prefix does), so only a page marked by hand can
    show it: a stranger's turn on a tainted page is not tainted, its note
    goes to external_public/, and the page stays marked."""
    model = FakeModel([
        [SpeechDelta("hi")],
        _calls(_NOTE), [SpeechDelta("noted")],
    ])
    ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
    await ml._run_one_turn([_stranger()])
    page = ml._contexts.get(PRIVATE)
    page.tainted = True
    await ml._run_one_turn([_stranger()])

    assert ml._contexts.get(PRIVATE) is page
    assert ml._ctx.tainted is False
    assert _tool_messages(model)[-1].startswith("memory noted: ")
    assert NOTE in _notes(tmp_path, "external_public")
    assert _notes(tmp_path, "external_owner") == ""
    assert page.tainted is True


# ---------------------------------------------------------------------------
# external_owner/ stays out of owner auto-injection; Recall scope
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, marker: str) -> None:
    (tmp_path / "shared" / "s.md").write_text(
        f"## h\n\nowner-note {marker}.\n", encoding="utf-8")
    (tmp_path / "external_owner" / "o.md").write_text(
        f"## h\n\npage-note {marker}.\n", encoding="utf-8")
    (tmp_path / "external_public" / "p.md").write_text(
        f"## h\n\nstranger-note {marker}.\n", encoding="utf-8")


async def test_external_owner_note_never_auto_injected_into_owner_turn(tmp_path, web):
    ms = _memsearch(tmp_path)
    try:
        (tmp_path / "shared" / "s.md").write_text(
            "## h\n\nowner-note unique-lh-818.\n", encoding="utf-8")
        await ms.index()
        model = FakeModel(
            [
                _calls(_FETCH), _calls(_NOTE), [SpeechDelta("saved it")],
                [SpeechDelta("hm")],
            ],
            usages=[(900, 50), (950, 30), (990, 20), (300, 10)],
        )
        ml = _loop(tmp_path, model, web, ms=ms, conversation_budget_tokens=1000)
        await ml._run_one_turn([_owner("save the page")])
        assert NOTE in _notes(tmp_path, "external_owner")

        # A new page: the page text is gone, and the note is not brought back.
        await ml._run_one_turn([_owner("what about unique-lh-818?")])
        owner_call = model.calls[3]
        assert owner_call["conversation"] == (PRIVATE, 2)
        owner_user = owner_call["messages"][-1]["content"]
        assert "[Memory context]" in owner_user
        assert "owner-note unique-lh-818" in owner_user     # retrieval ran
        whole = json.dumps(owner_call["messages"], ensure_ascii=False)
        assert NOTE not in whole
        assert "November 14" not in whole
        assert ml._ctx.tainted is False
    finally:
        ms.close()


async def test_owner_memory_hits_exclude_external_owner(tmp_path):
    """Unit form: what MindLoop asks memsearch for on an owner turn."""
    ms = _memsearch(tmp_path)
    try:
        _seed(tmp_path, "unique-mh-919")
        await ms.index()
        ml = make_mindloop(memory_root=tmp_path)
        ml._ctx.memsearch = ms
        hits = await ml._derive_memory_hits([_owner("unique-mh-919")])
        contents = " ".join(h["content"] for h in hits)
        assert "owner-note" in contents
        assert "page-note" not in contents and "stranger-note" not in contents
    finally:
        ms.close()


async def test_owner_turn_associative_search_excludes_external_owner(tmp_path, monkeypatch):
    import dollos.mind.mind_loop as mind_loop_mod

    seen: list[dict] = []

    async def spy(memsearch, mind_state, **kw):
        seen.append(kw)
        return []

    monkeypatch.setattr(mind_loop_mod, "associative_search", spy)
    ml = make_mindloop(memory_root=tmp_path)
    await ml._run_one_turn([_owner()])

    [kw] = seen
    assert kw["source_prefix"] is None
    assert set(kw["exclude_prefixes"]) == {
        tmp_path / "external_public", tmp_path / "external_owner",
    }


async def test_recall_reads_external_owner_only_with_include_external(tmp_path):
    ms = _memsearch(tmp_path)
    try:
        _seed(tmp_path, "unique-rc-929")
        await ms.index()
        model = FakeModel([
            _calls(("Recall", {"query": "unique-rc-929"})),
            _calls(("Recall", {"query": "unique-rc-929", "include_external": True}), _SHELL),
            [SpeechDelta("ok")],
        ])
        ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
        ml._ctx.memsearch = ms
        await ml._run_one_turn([_owner()])

        plain, external, shell = _tool_messages(model)
        assert "owner-note" in plain
        assert "page-note" not in plain and "stranger-note" not in plain
        [page] = [e for e in external.split("\n- ") if "page-note" in e]
        assert _EXTERNAL_READ in page
        assert "stranger-note" in external
        assert shell == f"Shell {_READ}; available now: NoteMemory, Recall"
        assert ml._ctx.tainted is True
        assert [o.summary for o in ml._state.recent_outputs if o.kind == "Recall"] == [
            "recalled: unique-rc-929",
            "recalled: unique-rc-929 [read external memory]",
        ]
    finally:
        ms.close()


@pytest.mark.parametrize("include_external", [False, True])
async def test_stranger_recall_never_reads_external_owner(tmp_path, include_external):
    ms = _memsearch(tmp_path)
    try:
        _seed(tmp_path, "unique-st-939")
        await ms.index()
        model = FakeModel([
            _calls(("Recall", {"query": "unique-st-939", "include_external": include_external})),
            "</think>ok",
        ])
        ml = make_mindloop(memory_root=tmp_path, llm=model, system_prompt="You are Doll.")
        ml._ctx.memsearch = ms
        await ml._run_one_turn([_stranger()])

        [msg] = _tool_messages(model)
        assert "stranger-note" in msg
        assert "page-note" not in msg and "owner-note" not in msg
    finally:
        ms.close()


# ---------------------------------------------------------------------------
# Tags, routing without a loop, the boot reindex
# ---------------------------------------------------------------------------


_EO_SOURCE = "data/memory/external_owner/2026-10-28.md"


def test_external_owner_hits_are_tagged_as_external_content():
    hit = {"content": "launch moved", "source": _EO_SOURCE}
    assert _EXTERNAL_READ in _render_memory([hit])
    assert _EXTERNAL_READ in _format_hit(hit)
    assert _EXTERNAL_READ in _render_associative(
        [{**hit, "_axis": "tod", "_axis_value": "morning"}]
    )
    sibling = {"content": "x", "source": "data/memory/external_owner_x/2026-10-28.md"}
    assert _EXTERNAL_READ not in _render_memory([sibling])
    assert _EXTERNAL_READ not in _format_hit(sibling)


@pytest.mark.parametrize(("tainted", "subdir"), [
    (False, "shared"), (True, "external_owner"),
])
async def test_note_memory_routes_on_ctx_tainted(tmp_path, tainted, subdir):
    ctx = _make_mind_ctx(tmp_path)
    ctx.tainted = tainted
    await NoteMemory(text="a fact").run(ctx)
    assert "a fact" in _notes(tmp_path, subdir)


async def test_executor_names_the_page_as_the_reason(tmp_path):
    ctx = _make_mind_ctx(tmp_path)
    ctx.tainted = ctx.tainted_by_page = True
    tools = {"Recall": Recall, "Shell": Shell, "NoteMemory": NoteMemory}
    ex = ToolExecutor(ctx, offered=tools, bound=tools, principal="owner",
                      kind="conversation")
    r = await ex.run("Shell", {"command": "ls"})
    assert r.unbound
    assert r.detail == f"Shell {_PAGE}; available now: NoteMemory, Recall"


def test_boot_reindex_covers_external_owner(tmp_path):
    from dollos.kernel import build_memsearch

    settings = _make_settings(tmp_path)
    mem = build_memsearch(settings)
    try:
        expected = tmp_path / "data" / "memory" / "external_owner"
        assert expected.is_dir()
        assert expected.resolve() in [Path(p).resolve() for p in mem._paths]
    finally:
        mem.close()
