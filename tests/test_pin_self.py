"""Tests for PinSelf — reflection-gated self-profile pin/replace/remove tool."""

import json
from pathlib import Path

import pytest

from dollos.mind.mind_ctx import MindCtx
from dollos.mind.self_profile import SECTION_TITLES
from dollos.mind.mind_state import MindState
from dollos.mind.sink_resolver import SinkResolver
from dollos.tool_outputs import ToolOutputStore
from dollos.harness.turn_policy import POLICIES
from dollos.tools import MAIN_TOOLS, NoteMemory, PinSelf


REFLECTION_SECTIONS = frozenset({"self", "relationship", "user"})
CONVERSATION_SECTIONS = frozenset({"user", "relationship"})


class _FakeShellRunner:
    def spawn(self, **kwargs):
        pass


class _FakeWorkflowRunner:
    def spawn(self, **kwargs):
        pass


class _FakeMonitorRunner:
    def spawn(self, **kwargs):
        return "mon-1"

    async def remove(self, monitor_id):
        return True


class _FakeMemSearch:
    """Fake memsearch — records every index_file call so tests can prove
    self_profile.md is NEVER indexed (always-inject, not recall)."""

    def __init__(self) -> None:
        self.indexed_sources: list[Path] = []

    async def index_file(self, path):
        self.indexed_sources.append(Path(path))

    async def search(self, query: str, top_k: int = 5):
        return []


@pytest.fixture
def make_mind_ctx(tmp_path):
    """Builds a real MindCtx with a tmp memory_root and a fake memsearch that
    records index_file calls. Everything else is the same real/near-real
    plumbing _dispatcher_helpers.py uses elsewhere (real MindState, real
    ToolOutputStore, fake shell/workflow/monitor runners)."""

    def _factory(self_profile_max_chars: int = 1200,
                 pin_sections: frozenset[str] = REFLECTION_SECTIONS) -> MindCtx:
        # Defaults to a reflection turn's sections (all three), where PinSelf
        # was the only place it ran before remember-corrections Task 2.
        return MindCtx(
            mind_state=MindState(),
            memsearch=_FakeMemSearch(),
            memory_root=tmp_path,
            transcripts_root=tmp_path / "transcripts",
            sink_resolver=SinkResolver(),
            tool_output_store=ToolOutputStore(tmp_path / "tool_outputs"),
            shell_runner=_FakeShellRunner(),
            workflow_runner=_FakeWorkflowRunner(),
            monitor_runner=_FakeMonitorRunner(),
            self_profile_max_chars=self_profile_max_chars,
            pin_sections=pin_sections,
        )

    return _factory


def test_pinself_in_reflection_not_main():
    assert PinSelf.__name__ in {e.tool for e in POLICIES["reflection"].extra_tools}
    assert PinSelf not in MAIN_TOOLS


@pytest.mark.asyncio
async def test_pinself_add_writes_file_and_not_indexed(make_mind_ctx):
    ctx = make_mind_ctx()
    tool = PinSelf(section="self", op="add", target="", text="我重視誠實")
    msg = await tool.run(ctx)
    prof = ctx.memory_root / "self_profile.md"
    assert prof.exists()
    assert "我重視誠實" in prof.read_text()
    assert "s1" in msg
    # 絕不 index:fake memsearch 不應收到 self_profile.md,且根本沒被呼叫過。
    assert all("self_profile.md" not in str(s) for s in ctx.memsearch.indexed_sources)
    assert ctx.memsearch.indexed_sources == []


@pytest.mark.asyncio
async def test_pinself_cap_returns_friendly_error(make_mind_ctx):
    """Verify that self-profile cap is enforced only AFTER accumulation.

    The cap is the empty file (three section titles) plus one and a half
    bullets of 40 chars:
    - First add serializes to scaffold + one bullet → succeeds (no error)
    - Second add would serialize to scaffold + two bullets → fails (has 'at its limit')
    This ensures we test the accumulate-then-cap path, not immediate cap-on-first."""
    from dollos.mind import self_profile as sp

    scaffold = sum(len(f"## {t}\n") for t in sp.SECTION_TITLES.values())
    bullet = len("- [s1·2026-01-01] \n") + 40
    ctx = make_mind_ctx(self_profile_max_chars=scaffold + bullet + bullet // 2)

    # First add should succeed
    msg1 = await PinSelf(section="self", op="add", target="", text="字" * 40).run(ctx)
    assert "at its limit" not in msg1, f"First add should not hit cap, but got: {msg1}"
    assert "s1" in msg1, f"First add should return success with id s1, but got: {msg1}"

    # Second add should fail due to accumulated total. Text must differ from
    # the first add (same length) — add() now dedups identical text within the
    # same section as a no-op, so an identical second add would short-circuit
    # to "already exists" before ever reaching the cap check.
    msg2 = await PinSelf(section="self", op="add", target="", text="字" * 39 + "2").run(ctx)
    assert "at its limit" in msg2, f"Second add should hit cap, but got: {msg2}"


def test_pinself_docstring_states_subject_test_as_criterion():
    """Docstring's dividing line between PinSelf and NoteMemory is the
    sentence's subject (about-you vs. about-the-world), not durability."""
    doc = PinSelf.__doc__
    assert "The test is the subject" in doc
    assert "NoteMemory" in doc


def test_pinself_docstring_allows_write_loose_nascent_entries():
    """Nascent/not-yet-sure-if-durable entries may be pinned — the write-time
    durability gate from the old docstring ('CORE, DURABLE truth... exactly
    three things') must be gone."""
    doc = PinSelf.__doc__
    assert "just budding" in doc
    assert "CORE, DURABLE" not in doc
    assert "exactly three things" not in doc


def test_pinself_docstring_frames_prune_as_selection_not_hygiene():
    """Pruning is reframed as 'what survives is your core', not cleanup."""
    doc = PinSelf.__doc__
    assert "what survives is your core" in doc


def test_pinself_docstring_allows_dual_recording_same_experience():
    """One experience can produce a NoteMemory entry (the fact) AND a
    PinSelf entry (what it revealed about her) — this must be explicit."""
    doc = PinSelf.__doc__
    assert "recorded in both" in doc


def test_pinself_section_field_mentions_opinions_and_interests():
    desc = PinSelf.model_fields["section"].description
    assert "opinions" in desc
    assert "interests" in desc
    assert "relationship=you and your owner" in desc
    assert "user=what you notice about your owner" in desc


def test_notememory_docstring_uses_subject_test_not_durability_gate():
    """NoteMemory's reciprocal boundary statement must match PinSelf's
    subject-test framing — the old write-time durability gate ('durable,
    defining truths') contradicted it (final-review Important finding)."""
    doc = NoteMemory.__doc__
    assert "subject" in doc
    assert "durable, defining truths" not in doc
    assert "just budding" in doc


# ---------------------------------------------------------------------------
# Which sections a turn may write (remember-corrections plan Task 2,
# decision 2; spec §2.1): a conversation turn writes user / relationship
# only — the self section is revised during reflection. The check is on the
# entry a replace / remove target RESOLVES to, because _find searches all
# three sections.
# ---------------------------------------------------------------------------


async def _seed(make_mind_ctx) -> tuple[Path, Path]:
    """One entry per section, written on a reflection turn: s1, r1, u1."""
    ctx = make_mind_ctx()
    for section in ("self", "relationship", "user"):
        await PinSelf(section=section, op="add", target="", text=f"{section} one").run(ctx)
    return ctx.memory_root / "self_profile.md", ctx.memory_root / "self_history.jsonl"


@pytest.mark.asyncio
@pytest.mark.parametrize(("section", "op", "target"), [
    ("self", "add", ""),
    ("self", "replace", "s1"),
    ("self", "remove", "s1"),
    # section says user, but the target is a self-section entry.
    ("user", "replace", "s1"),
    ("relationship", "remove", "s1"),
    ("user", "replace", "self one"),           # located by its text
])
async def test_conversation_turn_cannot_touch_the_self_section(
    make_mind_ctx, section, op, target,
):
    profile, history = await _seed(make_mind_ctx)
    before, before_history = profile.read_text(), history.read_text()
    ctx = make_mind_ctx(pin_sections=CONVERSATION_SECTIONS)

    msg = await PinSelf(section=section, op=op, target=target, text="new").run(ctx)

    assert msg == ("The self section is only revised during reflection; use the "
                   "user or relationship section for what your owner told you.")
    assert profile.read_text() == before
    assert history.read_text() == before_history


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["user", "relationship"])
async def test_conversation_turn_writes_user_and_relationship(make_mind_ctx, section):
    profile, history = await _seed(make_mind_ctx)
    ctx = make_mind_ctx(pin_sections=CONVERSATION_SECTIONS)
    first = section[0]

    added = await PinSelf(section=section, op="add", target="", text="two").run(ctx)
    replaced = await PinSelf(section=section, op="replace", target=f"{first}1",
                             text="one, revised").run(ctx)
    removed = await PinSelf(section=section, op="remove", target=f"{first}2",
                            text="").run(ctx)

    assert (added, replaced, removed) == (
        f'Pinned to "{SECTION_TITLES[section]}": {first}2',
        f"Updated {first}1", f"Removed {first}2",
    )
    text = profile.read_text()
    assert f"{section} one" not in text and "one, revised" in text
    assert "self one" in text
    kinds = [json.loads(line)["kind"] for line in history.read_text().splitlines()]
    assert kinds[-3:] == ["pin_add", "pin_replace", "pin_remove"]


@pytest.mark.asyncio
async def test_conversation_turn_target_that_matches_nothing_is_not_found(make_mind_ctx):
    """A target that resolves to nothing is apply's own not-found result,
    not the self-section refusal."""
    await _seed(make_mind_ctx)
    ctx = make_mind_ctx(pin_sections=CONVERSATION_SECTIONS)
    msg = await PinSelf(section="user", op="replace", target="u9", text="x").run(ctx)
    assert msg.startswith('No entry matches "u9"')


@pytest.mark.asyncio
async def test_reflection_turn_still_writes_all_three_sections(make_mind_ctx):
    profile, _ = await _seed(make_mind_ctx)
    ctx = make_mind_ctx()
    for section in ("self", "relationship", "user"):
        first = section[0]
        assert await PinSelf(section=section, op="replace", target=f"{first}1",
                             text=f"{section} revised").run(ctx) == f"Updated {first}1"
    assert await PinSelf(section="self", op="remove", target="s1",
                         text="").run(ctx) == "Removed s1"
    text = profile.read_text()
    assert "self revised" not in text
    assert "relationship revised" in text and "user revised" in text


@pytest.mark.asyncio
async def test_a_ctx_with_no_pin_sections_writes_nothing(make_mind_ctx, tmp_path):
    """MindCtx's default (an agent's ctx) allows no section at all."""
    ctx = make_mind_ctx(pin_sections=frozenset())
    msg = await PinSelf(section="user", op="add", target="", text="x").run(ctx)
    assert msg == "The user section can't be revised in this turn."
    assert not (tmp_path / "self_profile.md").exists()
