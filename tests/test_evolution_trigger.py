"""EvolutionTrigger Mode B re-verdict + skeptic scope + verdict_errors bound (spec §3.3)."""
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


def _trigger(tmp_path, *, verdict, consolidation_running=False, monkeypatch=None):
    memory_root = tmp_path
    trig = EvolutionTrigger(
        state=_StubState(),
        adapter=object(), renderer=object(), memsearch=object(),
        memory_root=memory_root, transcripts_root=tmp_path,
        tool_output_store=object(),
        pack_identity=Identity(self="You are Gura.", personality="p", taboos="t"),
        consolidation_trigger=types.SimpleNamespace(
            current_task=(object() if consolidation_running else None)),
        idle_threshold_s=600,
    )

    async def _fake_skeptic(**kw):
        if verdict == "error":
            raise RuntimeError("skeptic boom")
        return verdict
    if monkeypatch is not None:
        monkeypatch.setattr(trig, "_skeptic", _fake_skeptic)
    return trig


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
    counter = evo.to_counter(base, new_text="牴觸核心的改寫"+"字"*88, created_ts_now=1.0)
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
    trig = _trigger(tmp_path, verdict="pass", consolidation_running=True)
    trig._state.last_user_at = trig._state.last_iter_at = 0.0
    assert trig._should_reverdict(now=10_000.0) is False  # consolidation running
    trig2 = _trigger(tmp_path, verdict="pass", consolidation_running=False)
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
