"""Daily profile keeper (remember-corrections plan Task 4, spec §2.2/§2.3,
decisions 3–6).

- ``EditOwnerProfile``: the keeper's own tool. It writes only the user /
  relationship sections, and every change must quote the owner's words from
  the day's transcript, which the tool reads from disk (``ctx.owner_transcript``);
  a quote that is only in a ``Me:`` line, only in an action-log line, too
  short, or nowhere is refused with the file and the history unchanged.
- ``run_profile_keeper``: one keeper agent over the day's transcript.
- ``ConsolidationTrigger``: the keeper runs after a successful consolidation
  for the same date, never after a failed one, never when disabled, and its
  errors never change consolidation's result.
- Doll never sees the keeper's tool.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dollos.mind import self_profile
from tests._dispatcher_helpers import _FakeMemSearch, _make_mind_ctx
from tests._fake_model import FakeModel

DATE = "2026-10-26"

TRANSCRIPT = (
    "- 09:00:00 Owner: morning\n"
    "- 09:00:05 Me: Good morning, master.\n"
    "- 09:01:00 Owner: please   don't call me master,\n"
    "call me Alex\n"
    "- 09:02:00 ▸ noted: the owner dislikes cilantro\n"
    "- 09:03:00 Me: My owner dislikes cilantro, I think.\n"
    "- 09:04:00 Owner: ok\n"
)

PROFILE = (
    "## What I've learned about myself\n"
    "- [s1·2026-10-01] I like the sea\n"
    "## Me and my owner\n"
    "- [r1·2026-10-01] I call my owner master\n"
    "## What I've noticed about my owner\n"
    "- [u1·2026-10-01] My owner works late\n"
)


def _setup(tmp_path: Path, *, transcript: str | None = TRANSCRIPT):
    troot = tmp_path / "memory" / "transcripts"
    troot.mkdir(parents=True)
    if transcript is not None:
        (troot / f"{DATE}.md").write_text(transcript, encoding="utf-8")
    mroot = tmp_path / "memory"
    (mroot / "self_profile.md").write_text(PROFILE, encoding="utf-8")
    return mroot, troot


def _ctx(tmp_path, mroot, troot, *, transcript_path="default"):
    ctx = _make_mind_ctx(tmp_path)
    ctx.memory_root = mroot
    ctx.transcripts_root = troot
    if transcript_path == "default":
        ctx.owner_transcript = troot / f"{DATE}.md"
    else:
        ctx.owner_transcript = transcript_path
    return ctx


def _history(mroot: Path) -> list[dict]:
    p = mroot / "self_history.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _edit(**kw):
    from dollos.tools import EditOwnerProfile
    base = dict(section="relationship", op="add", target="", text="", quote="")
    base.update(kw)
    return EditOwnerProfile(**base)


# ---------------------------------------------------------------------------
# EditOwnerProfile: the quote check (decision 4)
# ---------------------------------------------------------------------------


async def test_quote_from_an_owner_line_writes_and_logs_by_and_quote(tmp_path):
    mroot, troot = _setup(tmp_path)
    ctx = _ctx(tmp_path, mroot, troot)
    result = await _edit(
        op="replace", target="r1", text="My owner goes by Alex, not master",
        quote="call me Alex",
    ).run(ctx)
    assert result == "Updated r1"
    assert "My owner goes by Alex, not master" in (mroot / "self_profile.md").read_text()
    [ev] = _history(mroot)
    assert ev["kind"] == "pin_replace"
    assert ev["by"] == "profile_keeper"
    assert ev["quote"] == "call me Alex"


async def test_quote_matches_after_whitespace_normalization(tmp_path):
    # The owner's line has a run of spaces and a line break inside it; the
    # quote has single spaces and stray padding.
    mroot, troot = _setup(tmp_path)
    ctx = _ctx(tmp_path, mroot, troot)
    result = await _edit(
        op="add", section="user", text="My owner goes by Alex",
        quote="  please don't call me master, call me Alex ",
    ).run(ctx)
    assert result.startswith("Pinned to")
    [ev] = _history(mroot)
    assert ev["quote"] == "please don't call me master, call me Alex"


@pytest.mark.parametrize("quote", [
    "Good morning, master",              # only in a Me: line
    "My owner dislikes cilantro",        # only in a Me: line
    "the owner dislikes cilantro",       # only in an action-log line
    "ok",                                # an owner line, but under 4 chars
    "call me Bob",                       # nowhere
    "",                                  # empty
])
async def test_quote_not_from_the_owner_is_refused(tmp_path, quote):
    mroot, troot = _setup(tmp_path)
    ctx = _ctx(tmp_path, mroot, troot)
    before = (mroot / "self_profile.md").read_text()
    result = await _edit(
        op="add", section="user", text="My owner dislikes cilantro", quote=quote,
    ).run(ctx)
    assert "quote" in result.lower()
    assert (mroot / "self_profile.md").read_text() == before
    assert _history(mroot) == []


@pytest.mark.parametrize("path", [None, "missing"])
async def test_no_transcript_means_no_edit(tmp_path, path):
    mroot, troot = _setup(tmp_path)
    tp = None if path is None else troot / "2026-01-01.md"
    ctx = _ctx(tmp_path, mroot, troot, transcript_path=tp)
    before = (mroot / "self_profile.md").read_text()
    result = await _edit(
        op="add", section="user", text="My owner goes by Alex", quote="call me Alex",
    ).run(ctx)
    assert "transcript" in result.lower()
    assert (mroot / "self_profile.md").read_text() == before
    assert _history(mroot) == []


# ---------------------------------------------------------------------------
# EditOwnerProfile: only user / relationship (decision 3)
# ---------------------------------------------------------------------------


def test_section_outside_user_and_relationship_is_a_validation_error():
    with pytest.raises(ValidationError):
        _edit(section="self", text="x", quote="call me Alex")


@pytest.mark.parametrize("op", ["replace", "remove"])
async def test_target_in_the_self_section_is_refused(tmp_path, op):
    mroot, troot = _setup(tmp_path)
    ctx = _ctx(tmp_path, mroot, troot)
    before = (mroot / "self_profile.md").read_text()
    result = await _edit(
        op=op, section="user", target="s1",
        text="" if op == "remove" else "x", quote="call me Alex",
    ).run(ctx)
    assert "self section" in result
    assert (mroot / "self_profile.md").read_text() == before
    assert _history(mroot) == []


async def test_apply_without_history_extra_logs_events_as_before(tmp_path):
    # Decision 6: PinSelf's events carry no by / quote.
    hp = tmp_path / "h.jsonl"
    self_profile.apply(tmp_path / "p.md", section="user", op="add", target="",
                       text="x", max_chars=4000, today=DATE, history_path=hp)
    [ev] = [json.loads(ln) for ln in hp.read_text().splitlines()]
    assert set(ev) == {"ts", "kind", "turn", "external_ctx", "section", "id", "text"}


# ---------------------------------------------------------------------------
# run_profile_keeper end to end (FakeModel)
# ---------------------------------------------------------------------------


class _FakeRenderer:
    def render(self, template_name: str, **ctx) -> str:
        return f"[stub system: {template_name}]"


def _call(name: str, args: dict) -> str:
    return f'<tool_call>{json.dumps({"name": name, "arguments": args})}</tool_call>'


_REPORT = _call("Report", {"status": "ok", "summary": "s", "details": "d"})


async def _run_keeper(tmp_path, mroot, troot, model, **kw):
    from dollos.mind.profile_keeper import run_profile_keeper
    from dollos.tool_outputs import ToolOutputStore
    return await run_profile_keeper(
        target_date=DATE, model=model, renderer=_FakeRenderer(),
        memsearch=_FakeMemSearch(), memory_root=mroot, transcripts_root=troot,
        tool_output_store=ToolOutputStore(tmp_path / "out"),
        max_tokens=2048, think_budget_tokens=300, transcript_tail_chars=8000, **kw,
    )


async def test_keeper_edits_the_profile_then_reports(tmp_path):
    from dollos.llm.tool_schema import tool_schemas
    from dollos.tools import EditOwnerProfile, Report

    mroot, troot = _setup(tmp_path)
    model = FakeModel([
        _call("EditOwnerProfile", {
            "section": "relationship", "op": "replace", "target": "r1",
            "text": "I call my owner Alex", "quote": "call me Alex",
        }),
        _REPORT,
    ])
    assert await _run_keeper(tmp_path, mroot, troot, model) is True

    profile = (mroot / "self_profile.md").read_text()
    assert "- [r1·" in profile and "] I call my owner Alex" in profile
    assert "I call my owner master" not in profile
    [ev] = _history(mroot)
    assert (ev["by"], ev["quote"]) == ("profile_keeper", "call me Alex")

    first = model.calls[0]
    assert first["purpose"] == "profile_keeper"
    assert first["tools"] == tool_schemas([EditOwnerProfile, Report])
    assert (first["max_tokens"], first["think_budget"]) == (2048, 300)
    task = first["messages"][1]["content"]
    # The day's conversation, without its action-log lines, and the owner
    # sections with their ids — never the self section.
    assert "Owner: please   don't call me master," in task
    assert "noted: the owner dislikes cilantro" not in task
    assert "r1: I call my owner master" in task
    assert "u1: My owner works late" in task
    assert "I like the sea" not in task


async def test_keeper_with_nothing_to_change_only_reports(tmp_path):
    mroot, troot = _setup(tmp_path)
    before = (mroot / "self_profile.md").read_text()
    assert await _run_keeper(tmp_path, mroot, troot, FakeModel([_REPORT])) is True
    assert (mroot / "self_profile.md").read_text() == before
    assert _history(mroot) == []


async def test_keeper_without_a_transcript_does_not_run(tmp_path):
    mroot, troot = _setup(tmp_path, transcript=None)
    model = FakeModel([_REPORT])
    assert await _run_keeper(tmp_path, mroot, troot, model) is False
    assert model.calls == []


async def test_keeper_that_never_reports_is_not_a_completed_run(tmp_path):
    mroot, troot = _setup(tmp_path)
    assert await _run_keeper(tmp_path, mroot, troot, FakeModel(["</think>done"])) is False


async def test_keeper_task_takes_the_tail_of_the_transcript(tmp_path):
    mroot, troot = _setup(tmp_path)
    model = FakeModel([_REPORT])
    from dollos.mind.profile_keeper import run_profile_keeper
    from dollos.tool_outputs import ToolOutputStore
    await run_profile_keeper(
        target_date=DATE, model=model, renderer=_FakeRenderer(),
        memsearch=_FakeMemSearch(), memory_root=mroot, transcripts_root=troot,
        tool_output_store=ToolOutputStore(tmp_path / "out"),
        max_tokens=2048, think_budget_tokens=300, transcript_tail_chars=20,
    )
    task = model.calls[0]["messages"][1]["content"]
    assert "09:04:00 Owner: ok" in task
    assert "Owner: morning" not in task


# ---------------------------------------------------------------------------
# ConsolidationTrigger runs the keeper after a successful consolidation
# ---------------------------------------------------------------------------


def _trigger(tmp_path, **over):
    from dollos.mind.consolidation import ConsolidationTrigger
    from dollos.mind.mind_state import MindState
    kw = dict(
        state=MindState(), persist_path=tmp_path / "s.json",
        model=object(), renderer=_FakeRenderer(), memsearch=_FakeMemSearch(),
        memory_root=tmp_path, transcripts_root=tmp_path / "transcripts",
        tool_output_store=object(), consolidated_dir=tmp_path / "consolidated",
        profile_keeper_enabled=True,
    )
    kw.update(over)
    return ConsolidationTrigger(**kw)


@pytest.fixture
def fakes(monkeypatch):
    import dollos.mind.consolidation as C
    seen = {"consolidation": [], "keeper": [], "consolidation_ok": True,
            "keeper_raises": None}

    async def fake_consolidation(*, target_date, **kw):
        seen["consolidation"].append(target_date)
        return seen["consolidation_ok"]

    async def fake_keeper(*, target_date, **kw):
        seen["keeper"].append((target_date, kw))
        if seen["keeper_raises"] is not None:
            raise seen["keeper_raises"]
        return True

    monkeypatch.setattr(C, "run_consolidation", fake_consolidation)
    monkeypatch.setattr(C, "run_profile_keeper", fake_keeper)
    return seen


async def test_trigger_runs_keeper_after_consolidation_succeeds(tmp_path, fakes):
    t = _trigger(tmp_path, profile_keeper_max_tokens=111,
                 profile_keeper_think_budget_tokens=222,
                 profile_keeper_transcript_tail_chars=333,
                 self_profile_max_chars=444)
    t._state.user_turn_count = 7
    assert await t._cycle(DATE) is True
    assert fakes["consolidation"] == [DATE]
    [(date, kw)] = fakes["keeper"]
    assert date == DATE
    assert (kw["max_tokens"], kw["think_budget_tokens"],
            kw["transcript_tail_chars"], kw["self_profile_max_chars"]) == (111, 222, 333, 444)
    assert t._state.last_consolidated_date == DATE
    assert t._state.last_consolidation_turn == 7


async def test_trigger_skips_keeper_when_consolidation_fails(tmp_path, fakes, caplog):
    # A day with no transcript is a failed consolidation too (run_consolidation
    # returns False for it).
    fakes["consolidation_ok"] = False
    t = _trigger(tmp_path)
    with caplog.at_level("INFO", logger="dollos.mind.consolidation"):
        assert await t._cycle(DATE) is False
    assert fakes["keeper"] == []
    assert any("profile keeper" in r.getMessage() for r in caplog.records)
    assert t._state.last_consolidated_date != DATE


async def test_trigger_skips_keeper_when_disabled(tmp_path, fakes):
    t = _trigger(tmp_path, profile_keeper_enabled=False)
    assert await t._cycle(DATE) is True
    assert fakes["consolidation"] == [DATE]
    assert fakes["keeper"] == []


async def test_keeper_error_leaves_consolidation_result_and_state(tmp_path, fakes, caplog):
    fakes["keeper_raises"] = RuntimeError("boom")
    t = _trigger(tmp_path)
    t._state.user_turn_count = 3
    with caplog.at_level("ERROR", logger="dollos.mind.consolidation"):
        assert await t._cycle(DATE) is True
    assert fakes["keeper"]
    assert t._state.last_consolidated_date == DATE
    assert t._state.last_consolidation_turn == 3
    assert any("profile keeper" in r.getMessage() for r in caplog.records)


async def test_keeper_cancel_propagates(tmp_path, fakes):
    # A returning owner cancels the keeper like consolidation (cancel_current);
    # consolidation's state is already saved.
    fakes["keeper_raises"] = asyncio.CancelledError()
    t = _trigger(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await t._cycle(DATE)
    assert t._state.last_consolidated_date == DATE


# ---------------------------------------------------------------------------
# Doll never sees the keeper's tool (decision 3)
# ---------------------------------------------------------------------------


def test_keeper_tool_is_never_offered_to_doll():
    from dollos.harness.permissions import TRUST_ORDER
    from dollos.harness.turn_policy import offered_tools
    from dollos.tools import MAIN_TOOLS, SUB_TOOLS, KEEPER_TOOLS

    names = {c.__name__ for c in MAIN_TOOLS}
    for principal in TRUST_ORDER:
        assert "EditOwnerProfile" not in offered_tools(
            principal, names, self_profile=True, evolution=True)
    assert "EditOwnerProfile" not in {c.__name__ for c in SUB_TOOLS + KEEPER_TOOLS}
    golden = Path(__file__).parent / "golden"
    for f in golden.glob("*.json"):
        assert "EditOwnerProfile" not in f.read_text(), f.name


def test_keeper_tool_has_a_permission_row():
    from dollos.harness.permissions import TOOL_MATRIX
    assert TOOL_MATRIX["EditOwnerProfile"] == frozenset({"owner", "owner_dm", "internal"})


# ---------------------------------------------------------------------------
# [profile_keeper] config (decision 5)
# ---------------------------------------------------------------------------


def test_profile_keeper_config_defaults_and_rejects_unknown_keys():
    from dollos.config import ConsolidationConfig, ProfileKeeperConfig
    c = ProfileKeeperConfig()
    assert c.enabled is True
    assert c.max_tokens == ConsolidationConfig().max_tokens
    assert c.transcript_tail_chars == ConsolidationConfig().transcript_tail_chars
    assert c.think_budget_tokens == 1024
    with pytest.raises(ValidationError):
        ProfileKeeperConfig(interval_s=10)


def test_keepers_read_a_whole_normal_day_by_default():
    """A normal day (~50 owner messages) is 15–20k chars; an early correction
    must reach the daily keepers, so the default tail fits a whole day, and
    the agent timeout covers prefilling it (Strata ~310 tok/s: 40k chars ≈
    21k tokens ≈ 70 s cold) plus a few tool calls."""
    import inspect

    from dollos.config import ConsolidationConfig, ProfileKeeperConfig
    from dollos.mind.consolidation import ConsolidationTrigger, run_consolidation
    from dollos.mind.profile_keeper import run_profile_keeper

    def default(fn, name):
        return inspect.signature(fn).parameters[name].default

    assert ConsolidationConfig().transcript_tail_chars == 40_000
    assert ProfileKeeperConfig().transcript_tail_chars == 40_000
    assert ConsolidationConfig().agent_timeout_s == 300
    assert default(run_consolidation, "transcript_tail_chars") == 40_000
    assert default(run_profile_keeper, "transcript_tail_chars") == 40_000
    assert default(ConsolidationTrigger, "transcript_tail_chars") == 40_000
    assert default(ConsolidationTrigger, "profile_keeper_transcript_tail_chars") == 40_000
    assert default(ConsolidationTrigger, "agent_timeout_s") == 300
