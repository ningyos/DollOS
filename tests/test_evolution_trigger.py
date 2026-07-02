"""EvolutionTrigger Mode B re-verdict + skeptic scope + verdict_errors bound (spec §3.3)."""
import asyncio
import types

import pytest

from dollos.character import Identity
from dollos.mind import evolution as evo
from dollos.mind import self_history
from dollos.mind.evolution_trigger import EvolutionTrigger


class _StubState:
    def __init__(self):
        self.last_user_at = 0.0
        self.last_iter_at = 0.0
        # Plan 3 Mode-A fields (plan review C1): EvolutionTrigger.__init__
        # always bootstraps/reads these, even for Mode-B-only test constructions.
        self.last_evolution_attempt_at = 0.0
        self.evolution_interval_days = 0.0
        self.evolution_hwm = 0


class _StubRenderer:
    def render(self, name, **kw):
        return "SYSTEM"


def _trigger(tmp_path, *, verdict, consolidation_running=False, monkeypatch=None,
             renderer=None):
    memory_root = tmp_path
    trig = EvolutionTrigger(
        state=_StubState(),
        adapter=object(), renderer=(renderer or object()), memsearch=object(),
        memory_root=memory_root, transcripts_root=tmp_path,
        tool_output_store=object(),
        pack_identity=Identity(self="You are Gura.", personality="p", taboos="t"),
        consolidation_trigger=types.SimpleNamespace(
            current_task=(object() if consolidation_running else None)),
        idle_threshold_s=600,
        persist_path=tmp_path / "mind_state.json",
    )

    async def _fake_skeptic(**kw):
        if verdict == "error":
            raise RuntimeError("skeptic boom")
        return verdict
    if monkeypatch is not None:
        monkeypatch.setattr(trig, "_skeptic", _fake_skeptic)
    return trig


def _skeptic_trigger(tmp_path, monkeypatch, report):
    """Trigger whose REAL _skeptic runs against a stubbed run_agent."""
    from dollos.mind import evolution_trigger as et_mod

    async def _fake_run_agent(**kw):
        return report
    monkeypatch.setattr(et_mod, "run_agent", _fake_run_agent)
    return _trigger(tmp_path, verdict=None, renderer=_StubRenderer())


@pytest.mark.asyncio
async def test_mode_b_pass_promotes_to_awaiting_doll(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="pass", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp).status == "awaiting_doll"


@pytest.mark.asyncio
async def test_mode_b_kill_counter_reverts_to_fallback(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    base = evo.make_keeper_slot(candidate="原候選"+"字"*88, rationale="R",
                                hwm_before=3, created_ts=0.0)
    counter = evo.to_counter(base, new_text="牴觸核心的改寫"+"字"*88)
    evo.save_slot(sp, counter)
    trig = _trigger(tmp_path, verdict="kill:牴觸 identity", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    reverted = evo.load_slot(sp)
    assert reverted.status == "awaiting_doll" and reverted.candidate == "原候選"+"字"*88
    assert reverted.notice is not None
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_kill"


@pytest.mark.asyncio
async def test_mode_b_kill_external_restores_and_clears(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    cs = tmp_path / "current_self.md"
    cs.write_text("有人改的"+"字"*88, encoding="utf-8")
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="kill:牴觸 taboo", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp) is None
    assert not cs.exists()  # restored (bootstrap delete)


@pytest.mark.asyncio
async def test_verdict_errors_bound_expires(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    slot = evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0)
    slot.verdict_errors = evo.VERDICT_ERRORS_BOUND - 1  # one more error trips it
    evo.save_slot(sp, slot)
    trig = _trigger(tmp_path, verdict="error", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert evo.load_slot(sp) is None
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_expire"


@pytest.mark.asyncio
async def test_error_below_bound_increments_and_retains(tmp_path, monkeypatch):
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="error", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    slot = evo.load_slot(sp)
    assert slot is not None and slot.verdict_errors == 1
    assert slot.last_error_ts is not None  # cooldown anchor set (I3)
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_error"


def test_should_reverdict_gates(tmp_path):
    # Condition 1 (idle) + condition 4 (no consolidation) + error cooldown ONLY
    # (spec §3.3 Mode B + failure table).
    # Consolidation-running case gets its own memory root WITH a valid
    # awaiting_skeptic slot, so the False verdict is attributable to the
    # consolidation gate alone (not slot absence).
    root_a = tmp_path / "a"
    evo.save_slot(root_a / "self_evolution" / "pending.json",
                  evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(root_a, verdict="pass", consolidation_running=True)
    trig._state.last_user_at = trig._state.last_iter_at = 0.0
    assert trig._should_reverdict(now=10_000.0) is False  # consolidation running
    trig_ok = _trigger(root_a, verdict="pass", consolidation_running=False)
    assert trig_ok._should_reverdict(now=10_000.0) is True  # same setup, gate lifted

    root_b = tmp_path / "b"
    trig2 = _trigger(root_b, verdict="pass", consolidation_running=False)
    assert trig2._should_reverdict(now=10.0) is False  # not idle yet
    assert trig2._should_reverdict(now=10_000.0) is False  # idle but no awaiting_skeptic slot


def test_recent_skeptic_error_blocks_reverdict_until_cooldown(tmp_path):
    """Spec §3.3 failure table: 1h error-cooldown — a transient skeptic error
    must not be retried on the very next 5s poll (else 3 errors expire a valid
    slot in ~15s, review I3)."""
    trig = _trigger(tmp_path, verdict="pass")
    slot = evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0)
    slot.last_error_ts = 9_000.0
    evo.save_slot(tmp_path / "self_evolution" / "pending.json", slot)
    assert trig._should_reverdict(now=10_000.0) is False   # 1000s < 3600s cooldown
    assert trig._should_reverdict(now=13_000.0) is True    # cooldown elapsed


def test_in_memory_error_ts_blocks_when_slot_anchor_missing(tmp_path):
    """M4: a persistent IO failure can leave the slot WITHOUT a persisted
    last_error_ts (save_slot / evo_error append raised). The in-memory fallback
    must still enforce the 1h cooldown so the 5s poll can't retry-loop."""
    trig = _trigger(tmp_path, verdict="pass")
    # A valid awaiting_skeptic slot with NO persisted error anchor.
    evo.save_slot(tmp_path / "self_evolution" / "pending.json",
                  evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    # Simulate the error handler having set only the in-memory anchor.
    trig._last_error_ts = 9_000.0
    assert trig._should_reverdict(now=10_000.0) is False   # in-memory cooldown holds
    assert trig._should_reverdict(now=13_000.0) is True    # cooldown elapsed


@pytest.mark.asyncio
async def test_reverdict_error_sets_in_memory_anchor(tmp_path, monkeypatch):
    """M4: the skeptic-error path sets the in-memory anchor too, not just the
    slot field — so the cooldown survives even if the slot write is lost."""
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict="error", monkeypatch=monkeypatch)
    await trig._reverdict_once()
    assert trig._last_error_ts is not None


# --- _skeptic verdict parsing (real _skeptic, stubbed run_agent) ---

@pytest.mark.asyncio
@pytest.mark.parametrize("report", [None, {"summary": "s", "details": ""}])
async def test_skeptic_missing_verdict_raises(tmp_path, monkeypatch, report):
    """No Report / empty details is an ERROR (→ verdict_errors path), never a
    silent pass or kill."""
    trig = _skeptic_trigger(tmp_path, monkeypatch, report)
    with pytest.raises(RuntimeError):
        await trig._skeptic(old_sanctioned=None, proposed="新提案")


@pytest.mark.asyncio
async def test_skeptic_pass_report_parses(tmp_path, monkeypatch):
    trig = _skeptic_trigger(tmp_path, monkeypatch,
                            {"summary": "s", "details": "PASS 沒有牴觸 (a)(b)"})
    assert await trig._skeptic(old_sanctioned="舊文", proposed="新提案") == "pass"


@pytest.mark.asyncio
async def test_skeptic_kill_report_parses_reason(tmp_path, monkeypatch):
    trig = _skeptic_trigger(tmp_path, monkeypatch,
                            {"summary": "s", "details": "KILL 改名了"})
    assert await trig._skeptic(old_sanctioned="舊文", proposed="新提案") == "kill:改名了"


@pytest.mark.asyncio
async def test_skeptic_garbled_report_never_silent_pass(tmp_path, monkeypatch):
    """Ambiguous / garbled Report text (no leading PASS) must resolve to KILL —
    fail-closed, never a silent pass."""
    trig = _skeptic_trigger(tmp_path, monkeypatch,
                            {"summary": "s", "details": "嗯,我不太確定這個提案好不好"})
    verdict = await trig._skeptic(old_sanctioned="舊文", proposed="新提案")
    assert verdict.startswith("kill:")


# --- timeout enters the verdict_errors bound (review Important) ---

@pytest.mark.asyncio
async def test_skeptic_timeout_enters_error_bound(tmp_path, monkeypatch):
    """Spec §3.3 failure table lists timeout explicitly: a timing-out skeptic
    must consume the verdict_errors bound and set the 1h cooldown anchor —
    not retry forever outside the bound (review Important)."""
    sp = tmp_path / "self_evolution" / "pending.json"
    evo.save_slot(sp, evo.make_external_slot(candidate="有人改的"+"字"*88, created_ts=0.0))
    trig = _trigger(tmp_path, verdict=None)

    async def _hang(**kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(trig, "_skeptic", _hang)
    trig._agent_timeout_s = 0.05
    await trig._reverdict_once()
    slot = evo.load_slot(sp)
    assert slot is not None and slot.verdict_errors == 1  # bound consumed
    assert slot.last_error_ts is not None                 # cooldown anchor set
    assert self_history.read_events(tmp_path/"self_history.jsonl")[-1]["kind"] == "evo_error"
