"""Guard: the prompts that carry Doll's words hold no Chinese the daemon wrote.

Plans ``docs/superpowers/plans/2026-10-19-gura-english-speech-fix.md`` Task 1
and ``2026-10-19-doll-all-english.md`` Task 1. Doll talks and writes in
English; so does everything the daemon puts around her words: tool
descriptions, system scaffolding, labels, perception and channel framing, tool
results, the headings written into her memory files, and the prompts of the
agents that write for her (evolution keeper and skeptics, consolidation). The
only Chinese allowed is the sentinel ``她的字``, which stands in for her own
content (and other people's) wherever it can appear.

Each case renders one source the way the daemon does, from a real
``DollOS(settings)`` (gura pack, temp data root, evolution and self-profile
on). A new source of daemon text gets its own case here.
"""
from __future__ import annotations

import json
import re
import string
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pytest

from dollos.cascade.tool_loop import dispatch_one
from dollos.character import Identity
from dollos.config import (
    CharacterConfig,
    DataConfig,
    EnergyConfig,
    EvolutionConfig,
    IPCConfig,
    LLMConfig,
    SelfProfileConfig,
    Settings,
    TraceSettings,
)
from dollos.kernel import DollOS
from dollos.llm.tool_schema import tool_schemas
from dollos.memory_writer import append_action_log, append_transcript
from dollos.mind import consolidation
from dollos.mind import evolution as evo
from dollos.mind import evolution_keeper as ek
from dollos.mind import evolution_trigger
from dollos.mind import self_profile
from dollos.mind.action_log import action_phrase_for_perception, action_phrase_for_tool
from dollos.mind.mind_prompt import (
    Seen,
    energy_bucket_line,
    render_body_signal,
    render_diary_request,
    render_epoch_start,
    render_turn_delta,
)
from dollos.mind.mind_state import Mood, OpenLoop, OutputRecord, Perception
from dollos.mind.persona_guard import check_persona_violations
from dollos.mind.tool_memory import render_tool_outcomes
from dollos.perception.system_pulse import PulseSample
from dollos.tools import _format_hit
from tests._cjk import ForeignCJK, assert_only_allowed, cjk_runs

SENTINEL = "她的字"
ALLOWED = [SENTINEL]
GURA_PACK = Path(__file__).resolve().parents[1] / "character_packs" / "gura"


# --------------------------------------------------------------------------- #
# The helper itself                                                            #
# --------------------------------------------------------------------------- #


def test_cjk_runs_finds_ideographs_and_full_width_punctuation():
    # U+3400 (Extension A), U+F900 (compatibility ideograph)
    assert cjk_runs("hi 主人：ok「x」 㐀 豈") == ["主人：", "「", "」", "㐀", "豈"]
    assert cjk_runs("plain English — no CJK… ·") == []


def test_assert_only_allowed_passes_when_only_allowed_strings_carry_cjk():
    assert_only_allowed(f"note {SENTINEL} and {SENTINEL}.", ALLOWED)


def test_assert_only_allowed_removes_whole_strings_not_substrings():
    # 她 is inside the allowed sentinel, but a bare 她 is not allowed.
    with pytest.raises(ForeignCJK) as exc:
        assert_only_allowed(f"{SENTINEL} / your 她 here", ALLOWED)
    assert "她" in str(exc.value)
    assert "your 她 here" in str(exc.value)  # the context around it


# --------------------------------------------------------------------------- #
# Rig: a real kernel and the render inputs MindLoop would pass                 #
# --------------------------------------------------------------------------- #


@dataclass
class Rig:
    kernel: DollOS
    now: float

    @property
    def ml(self):
        return self.kernel._mind_loop

    @property
    def state(self):
        return self.kernel._mind_state

    @property
    def ctx(self):
        return self.kernel._mind_ctx

    @property
    def memory_root(self) -> Path:
        return self.ctx.memory_root

    # Same inputs as MindLoop._run_one_turn.
    def energy_line(self) -> str | None:
        return energy_bucket_line(self.state.energy) if self.ml._energy_enabled else None

    def self_profile_text(self) -> str | None:
        if not self.ml._self_profile_enabled:
            return None
        return self_profile.render_block(self.memory_root / "self_profile.md")

    def snapshot(self, seen: Seen | None = None) -> str:
        return render_epoch_start(
            self.state, seen if seen is not None else Seen(), "internal", self.now,
            self_profile_text=self.self_profile_text(), energy_line=self.energy_line(),
        )

    def delta(self, perceptions: list[Perception], **blocks) -> str:
        """The private turn message for ``perceptions`` on a page whose
        snapshot is current, so only this turn's own content renders."""
        seen = Seen()
        self.snapshot(seen)
        return render_turn_delta(
            self.state, seen, "internal", perceptions, self.now,
            self_profile_text=self.self_profile_text(), energy_line=self.energy_line(),
            **blocks,
        )

    async def drain(self) -> list[Perception]:
        return await self.kernel._perception_queue.drain(timeout_s=1)

    def fill(self) -> None:
        """Her own content everywhere it can be (mood is an English word)."""
        st = self.state
        st.mood = Mood(emotion="uneasy", reason=SENTINEL)
        st.focus = SENTINEL
        st.scratchpad = SENTINEL
        st.open_loops = [
            OpenLoop(id="g1", desc=SENTINEL, opened_at=self.now - 3600,
                     self_directed=True, trigger=SENTINEL, progress=[SENTINEL]),
            OpenLoop(id="l1", desc=SENTINEL, opened_at=self.now - 7200),
        ]
        st.recent_perceptions.append(
            Perception(kind="UserSpoke", t=self.now - 60, data={"text": SENTINEL}))
        st.recent_outputs.append(
            OutputRecord(t=self.now - 30, kind="Speech", summary=f"spoke: {SENTINEL}"))
        st.recent_outputs.append(
            OutputRecord(t=self.now - 20, kind="NoteMemory", summary=f"noted: {SENTINEL}"))
        path = self.memory_root / "self_profile.md"
        for section in ("self", "relationship", "user"):
            self_profile.apply(path, section=section, op="add", target="", text=SENTINEL,
                               max_chars=1200, today="2026-10-19")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    settings = Settings(
        llm=LLMConfig(base_url="http://127.0.0.1:9", model="m"),
        ipc=IPCConfig(host="127.0.0.1", port=0),
        data=DataConfig(root=tmp_path / "data"),
        character=CharacterConfig(pack=GURA_PACK),
        trace=TraceSettings(root=str(tmp_path / "traces")),
        energy=EnergyConfig(enabled=True),
        self_profile=SelfProfileConfig(enabled=True),
        evolution=EvolutionConfig(enabled=True),
    )
    return Rig(kernel=DollOS(settings), now=time.time())


# --------------------------------------------------------------------------- #
# Sources                                                                      #
# --------------------------------------------------------------------------- #

# Her current self and a pending candidate differ, so neither path short-cuts.
SANCTIONED = f"{SENTINEL} current"
CANDIDATE = f"{SENTINEL} proposed"


def _adopt(rig: Rig, text: str) -> None:
    evo.log_or_raise(rig.memory_root / "self_history.jsonl", kind=evo.EVO_ADOPT,
                     text=text, old_text=None, drift_score=None)


# ---- system ---------------------------------------------------------------- #


async def system_prompt(rig: Rig) -> str:
    return rig.ml._system_for_turn()


async def system_prompt_with_current_self(rig: Rig) -> str:
    _adopt(rig, SENTINEL)
    return rig.ml._system_for_turn()


# ---- tool schemas ---------------------------------------------------------- #


def _tools_json(rig: Rig, tier: str) -> str:
    rig.ctx.origin_tier = tier
    return json.dumps(tool_schemas(rig.ml._offered_tools().values()), ensure_ascii=False)


async def tools_internal(rig: Rig) -> str:
    return _tools_json(rig, "internal")


async def tools_external_public(rig: Rig) -> str:
    return _tools_json(rig, "external_public")


# ---- a new page's snapshot ------------------------------------------------- #


async def snapshot_default_state(rig: Rig) -> str:
    return rig.snapshot()


async def snapshot_filled_state(rig: Rig) -> str:
    rig.fill()
    return rig.snapshot()


# ---- turn deltas ----------------------------------------------------------- #


async def delta_awoke(rig: Rig) -> str:
    return rig.delta([
        Perception(kind="Awoke", t=rig.now, data={"reason": "cold_start"}),
        Perception(kind="Awoke", t=rig.now, data={"reason": "recovered"}),
    ])


async def delta_user_spoke(rig: Rig) -> str:
    return rig.delta([Perception(kind="UserSpoke", t=rig.now, data={"text": SENTINEL})])


async def delta_bootstrap_schedule(rig: Rig) -> str:
    await rig.kernel._maybe_bootstrap_plan()
    perceptions = await rig.drain()
    assert [p.kind for p in perceptions] == ["ScheduledMoment"]
    return rig.delta(perceptions)


async def delta_reflection(rig: Rig) -> str:
    """ReflectionMoment with the turn's [Tool outcomes] and a surfaced
    candidate (keeper, carrying a skeptic notice), as MindLoop renders it."""
    _adopt(rig, SANCTIONED)
    rig.state.tool_stats = {"Recall": {"ok": 2, "fail": 1}}
    slot = evo.make_keeper_slot(candidate=CANDIDATE, rationale=SENTINEL,
                                hwm_before=None, created_ts=rig.now)
    slot.notice = SENTINEL
    slot_path = rig.memory_root / "self_evolution" / "pending.json"
    evo.save_slot(slot_path, slot)
    hist = rig.memory_root / "self_history.jsonl"
    block = evo.surface_or_expire(
        slot_path=slot_path, history_path=hist,
        current_self_path=rig.memory_root / "current_self.md",
        sanctioned_text=SANCTIONED, max_surfacings=5, min_age_days=2.0, now=rig.now,
    )
    assert block is not None
    return rig.delta(
        [Perception(kind="ReflectionMoment", t=rig.now, data={"iters_since_last": 30})],
        tool_outcomes_block=render_tool_outcomes(
            rig.state.tool_stats, rig.state.recent_tool_failures),
        evolution_block=block,
    )


async def evolution_block_other_origins(rig: Rig) -> str:
    """The candidate block's other origin notes: her own rewrite (counter), an
    outside file edit (external), and the first version (nothing adopted)."""
    keeper = evo.make_keeper_slot(candidate=CANDIDATE, rationale=SENTINEL,
                                  hwm_before=None, created_ts=rig.now)
    counter = evo.mark_awaiting_doll(evo.to_counter(keeper, new_text=CANDIDATE))
    external = evo.mark_awaiting_doll(
        evo.make_external_slot(candidate=CANDIDATE, created_ts=rig.now))
    return "\n\n".join([
        evo.render_surfacing(slot=counter, sanctioned_text=SANCTIONED, reminder_n=1),
        evo.render_surfacing(slot=external, sanctioned_text=None, reminder_n=2),
    ])


async def delta_persona_drift(rig: Rig) -> str:
    said = f"{SENTINEL} a~ !!"
    violations = check_persona_violations(said, rig.kernel._doll_pack.enforcement)
    assert violations
    return rig.delta([Perception(kind="PersonaDriftDetected", t=rig.now,
                                 data={"violations": violations, "snippet": said[:120]})])


async def delta_bridge_and_mcp_down(rig: Rig) -> str:
    rig.kernel._emit_bridge_down_perception("discord-bridge", 1)
    rig.kernel._emit_mcp_down_perception("mcp-server", 1)
    perceptions = await rig.drain()
    assert [p.kind for p in perceptions] == ["BridgeDown", "McpDown"]
    return rig.delta(perceptions)


class _FixedPulse:
    def __init__(self, sample: PulseSample) -> None:
        self._sample = sample

    def latest_sample(self) -> PulseSample:
        return self._sample


async def delta_pulse(rig: Rig) -> str:
    """Every PulseObserver alert (battery, GPU, stuck window), each with the
    [Body signal] block of its severity, as a pure pulse turn renders it."""
    obs = rig.kernel._pulse_observer
    obs._system_pulse = _FixedPulse(PulseSample(
        taken_at=datetime.now(), gpus=[(50.0, 90.0)], battery_pct=5.0,
        battery_status="Discharging", active_window=SENTINEL, idle_s=0.0,
    ))
    pulse = rig.kernel.settings.system_pulse
    t0 = 1_000_000.0
    obs._tick(t0)                                         # battery
    obs._tick(t0 + pulse.alert_throttle_s + 1)            # gpu
    obs._tick(t0 + pulse.alert_throttle_s + pulse.window_stuck_s + 2)  # window
    perceptions = await rig.drain()
    assert sorted(p.data["concern"] for p in perceptions) == [
        "battery_critical", "gpu_hot", "window_stuck"]
    parts = []
    for severity in ("critical", "advisory"):
        wakes = [p for p in perceptions if p.data["severity"] == severity]
        body = render_body_signal([(p.data["severity"], p.data["detail"]) for p in wakes])
        parts.append(rig.delta(wakes, body_signal_block=body))
    return "\n\n".join(parts)


async def delta_tool_result(rig: Rig) -> str:
    return rig.delta([
        Perception(kind="ToolResultArrived", t=rig.now, data={
            "tool": "Shell", "task_id": "sh-1", "status": "ok",
            "summary": f"exit 0: {SENTINEL}", "output_id": "out-1", "line_count": 3}),
        Perception(kind="ToolResultArrived", t=rig.now, data={
            "tool": "Workflow", "task_id": "wf-1", "status": "ok", "summary": SENTINEL,
            "details_output_id": "out-2", "details_line_count": 9}),
    ])


async def delta_other_internal_kinds(rig: Rig) -> str:
    seen = Seen()
    rig.snapshot(seen)
    rig.state.safe_mode = True
    rig.state.safe_mode_reason = "3 consecutive tool failures"
    return render_turn_delta(rig.state, seen, "internal", [
        Perception(kind="MonitorFired", t=rig.now, data={"monitor_id": "mon-1", "line": SENTINEL}),
        Perception(kind="MonitorEnded", t=rig.now, data={"monitor_id": "mon-1", "exit_status": 0}),
        Perception(kind="ScheduledMoment", t=rig.now, data={"intent": SENTINEL}),
        Perception(kind="Interrupted", t=rig.now, data={"by": "user"}),
        Perception(kind="SafeModeEntered", t=rig.now,
                   data={"reason": "3 consecutive tool failures"}),
        Perception(kind="RepeatLoopDetected", t=rig.now,
                   data={"tool": "Recall", "summary": f"recalled: {SENTINEL}", "count": 3}),
        Perception(kind="AgendaMoment", t=rig.now, data={}),
    ], rig.now, self_profile_text=rig.self_profile_text(), energy_line=rig.energy_line())


async def delta_tool_habits(rig: Rig) -> str:
    lesson = f"## 2026-10-19 10:00:00\n\n[situation] {SENTINEL}\n{SENTINEL}\n"
    return rig.delta(
        [Perception(kind="UserSpoke", t=rig.now, data={"text": SENTINEL})],
        tool_habits_hits=[{"source": "tool_playbook.md", "content": lesson, "chunk_hash": "th1"}],
    )


async def delta_memory_hits_with_provenance(rig: Rig) -> str:
    public = str(rig.memory_root / "external_public" / "2026-10-19.md")
    shared = str(rig.memory_root / "shared" / "2026-10-19.md")
    return rig.delta(
        [Perception(kind="UserSpoke", t=rig.now, data={"text": SENTINEL})],
        memory_hits=[
            {"source": public, "content": SENTINEL, "chunk_hash": "m1"},
            {"source": shared, "content": SENTINEL, "chunk_hash": "m2"},
        ],
        associative_hits=[
            {"source": public, "content": SENTINEL, "chunk_hash": "a1",
             "_axis": "mood", "_axis_value": "uneasy"},
        ],
    )


# ---- the diary's one-off request ------------------------------------------- #


async def diary_request(rig: Rig) -> str:
    """render_diary_request with [Today's log] cut in the middle (her day's
    lines only, so this case is about the diary framing)."""
    rig.fill()
    log = rig.ctx.transcripts_root / f"{date.today():%Y-%m-%d}.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"- 09:00:00 {SENTINEL}\n" + "x" * 400 + f"\n- 22:00:00 {SENTINEL}\n",
                   encoding="utf-8")
    rig.ml._diary_max_log_chars = 100
    return render_diary_request(
        rig.state, [Perception(kind="DiaryMoment", t=rig.now, data={})], rig.now,
        today_log_block=rig.ml._read_today_log(),
        self_profile_text=rig.self_profile_text(), energy_line=rig.energy_line(),
    )


# ---- tool results ------------------------------------------------------------ #


async def _run_tool(rig: Rig, name: str, args: dict, registry: dict | None = None) -> str:
    registry = registry if registry is not None else rig.ml._offered_tools()
    result = await dispatch_one(name, args, rig.ctx, registry)
    assert result is not None
    return result.detail


async def tool_result_unknown_tool(rig: Rig) -> str:
    return await _run_tool(rig, "NoSuchTool", {})


async def tool_result_bad_arguments(rig: Rig) -> str:
    return await _run_tool(rig, "Shell", {"command": "true", "timeout_s": 9999})


class _StubRunner:
    def spawn(self, **kwargs) -> str:
        return "mon-1"


async def tool_result_dispatched(rig: Rig) -> str:
    rig.ctx.shell_runner = rig.ctx.workflow_runner = rig.ctx.monitor_runner = _StubRunner()
    return "\n".join([
        await _run_tool(rig, "Shell", {"command": "echo hi"}),
        await _run_tool(rig, "SpawnWorkflow", {"tasks": [{"task": "look it up"}]}),
        await _run_tool(rig, "SpawnMonitor", {"command": "tail -F log"}),
    ])


async def tool_result_invoke_skill_missing(rig: Rig) -> str:
    return await _run_tool(rig, "InvokeSkill", {"name": "nope"})


async def tool_result_recall_provenance(rig: Rig) -> str:
    return "\n".join([
        _format_hit({"source": str(rig.memory_root / "consolidated" / "c.md"),
                     "content": SENTINEL}),
        _format_hit({"source": str(rig.memory_root / "external_public" / "2026-10-19.md"),
                     "content": SENTINEL}),
    ])


async def tool_result_pin_self(rig: Rig) -> str:
    def pin(op: str, target: str = "", text: str = SENTINEL, section: str = "self"):
        return _run_tool(rig, "PinSelf",
                         {"section": section, "op": op, "target": target, "text": text})

    out = [
        await pin("add"),                       # pinned
        await pin("add"),                       # duplicate
        await pin("replace", target="s9"),      # not found, listing entries
        await pin("replace", target="s1"),      # updated
        await pin("remove", target="s1"),       # removed
        await pin("remove", target="s1"),       # not found, no entries left
    ]
    rig.ctx.self_profile_max_chars = 10
    out.append(await pin("add", section="user"))  # over the cap
    path = rig.memory_root / "self_profile.md"
    for section, op in (("nope", "add"), ("self", "nope")):
        out.append(self_profile.apply(path, section=section, op=op, target="",
                                      text=SENTINEL, max_chars=1200, today="2026-10-19"))
    return "\n".join(out)


async def tool_result_self_revision(rig: Rig) -> str:
    """Every SelfRevision result, success and refusal."""
    ctx = rig.ctx
    slot_path = rig.memory_root / "self_evolution" / "pending.json"
    out: list[str] = []

    def keeper(candidate: str = CANDIDATE) -> evo.PendingSlot:
        return evo.make_keeper_slot(candidate=candidate, rationale=SENTINEL,
                                    hwm_before=None, created_ts=rig.now)

    async def revise(args: dict, *, slot: evo.PendingSlot | None = None,
                     surfaced: bool = True, latched: bool = False) -> None:
        evo.clear_slot(slot_path)
        if slot is not None:
            evo.save_slot(slot_path, slot)
        ctx.evolution_latched = latched
        ctx.evolution_candidate_surfaced = surfaced
        out.append(await _run_tool(rig, "SelfRevision", args))

    # Sized from the rig's caps so each refusal below stays the one named.
    floor, cap = ctx.current_self_min_chars, ctx.current_self_max_chars
    long_text = "x" * (floor + 10)                      # passes the length check
    too_long = SENTINEL * (cap // len(SENTINEL) + 1)
    _adopt(rig, SANCTIONED)
    await revise({"decision": "adopt"}, slot=keeper(), latched=True)
    await revise({"decision": "adopt"})                               # no candidate
    in_review = keeper()
    in_review.status = "awaiting_skeptic"
    await revise({"decision": "adopt"}, slot=in_review)
    await revise({"decision": "adopt"}, slot=keeper(), surfaced=False)
    await revise({"decision": "reject", "reason": SENTINEL}, slot=keeper())
    await revise({"decision": "adopt"}, slot=keeper(candidate=SANCTIONED))  # zero move
    await revise({"decision": "adopt", "text": SANCTIONED}, slot=keeper())  # = current
    capped = keeper()
    capped.counter_round = evo.COUNTER_ROUND_CAP
    await revise({"decision": "adopt", "text": long_text}, slot=capped)
    await revise({"decision": "adopt", "text": f"{SENTINEL} short"}, slot=keeper())
    await revise({"decision": "adopt", "text": too_long}, slot=keeper())
    await revise({"decision": "adopt", "text": long_text + " a~"}, slot=keeper())
    await revise({"decision": "adopt", "text": long_text}, slot=keeper())   # sent to review

    def fail(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(evo, "log_or_raise", fail)
        await revise({"decision": "reject"}, slot=keeper())
        await revise({"decision": "adopt"}, slot=keeper())
        await revise({"decision": "adopt", "text": long_text}, slot=keeper())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("dollos.tools._atomic_write_text", fail)
        await revise({"decision": "adopt"}, slot=keeper())
    await revise({"decision": "adopt"}, slot=keeper(candidate=f"{SENTINEL} again"))
    text = "\n".join(out)
    for result in ("Too short", "Too long", "Breaks the persona constraints",
                   "gone to review", "rewrite failed"):
        assert result in text, result
    return text


async def tool_result_learn_name(rig: Rig) -> str:
    return "\n".join([
        await _run_tool(rig, "LearnName", {"op": "add", "token": SENTINEL}),
        await _run_tool(rig, "LearnName", {"op": "remove", "token": SENTINEL}),
        await _run_tool(rig, "LearnName", {"op": "add", "token": "a"}),
    ])


# ---- labels written into her memory files ------------------------------------ #

_TOOL_ACTIONS = [
    ("Shell", {"command": "echo hi"}),
    ("SpawnWorkflow", {"tasks": [{"task": "x"}]}),
    ("SpawnMonitor", {"command": "tail -F log"}),
    ("RemoveMonitor", {"monitor_id": "mon-1"}),
    ("PursueGoal", {"desc": SENTINEL}),
    ("AdvanceGoal", {"id": "g1", "progress": SENTINEL}),
    ("CloseLoop", {"id": "g1", "outcome": SENTINEL}),
    ("WriteSchedule", {"entries": [{}, {}, {}, {}]}),
    ("SelfRevision", {"decision": "adopt"}),
    ("PinSelf", {"op": "add", "section": "self"}),
    ("LearnName", {"op": "add", "token": SENTINEL}),
    ("NoteMemory", {"text": SENTINEL}),
    ("MoodTool", {"emotion": "uneasy", "reason": SENTINEL}),
]
_EVENT_ACTIONS = [
    ("ToolResultArrived", {"tool": "Shell", "task_id": "sh-1", "status": "ok",
                           "summary": SENTINEL}),
    ("MonitorFired", {"monitor_id": "mon-1", "line": SENTINEL}),
    ("MonitorEnded", {"monitor_id": "mon-1", "exit_status": 0}),
    ("BridgeDown", {"service": "discord-bridge", "rc": 1}),
    ("McpDown", {"service": "mcp-server", "rc": 1}),
    ("PulseMoment", {"detail": SENTINEL}),
]


async def memory_transcript_and_action_log(rig: Rig) -> str:
    """The day's transcript file: both speakers' lines and every action /
    event line (what Recall and [Today's log] give back to her)."""
    root, memsearch = rig.ctx.transcripts_root, rig.ctx.memsearch
    for role in ("user", "doll"):
        await append_transcript(transcripts_root=root, memsearch=memsearch,
                                role=role, text=SENTINEL)
    phrases = [action_phrase_for_tool(name, args, prior_mood_emotion="calm")
               for name, args in _TOOL_ACTIONS]
    phrases += [action_phrase_for_perception(kind, data) for kind, data in _EVENT_ACTIONS]
    assert all(phrases)
    for phrase in phrases:
        await append_action_log(transcripts_root=root, memsearch=memsearch, phrase=phrase)
    return (root / f"{date.today():%Y-%m-%d}.md").read_text(encoding="utf-8")


async def _write_her_files(rig: Rig) -> None:
    """A note, her diary, a schedule and self-profile pins (one while reading
    external content), written by her tools as a turn would."""
    await _run_tool(rig, "NoteMemory", {"text": SENTINEL})
    await _run_tool(rig, "WriteDiary", {"content": SENTINEL})
    await _run_tool(rig, "WriteSchedule",
                    {"entries": [{"time": "09:00:00", "intent": SENTINEL}]})
    for section in ("self", "relationship", "user"):
        await _run_tool(rig, "PinSelf", {"section": section, "op": "add", "target": "",
                                         "text": f"{SENTINEL} {section}"})
    rig.ctx.external_ctx = True
    await _run_tool(rig, "PinSelf", {"section": "self", "op": "add", "target": "",
                                     "text": f"{SENTINEL} outside"})


async def memory_her_files(rig: Rig) -> str:
    """Her day file and self_profile.md, whole: the headings the daemon writes."""
    await _write_her_files(rig)
    return "\n".join(
        (rig.memory_root / name).read_text(encoding="utf-8")
        for name in (f"shared/{date.today():%Y-%m-%d}.md", "self_profile.md")
    )


# ---- the agents that write for her ------------------------------------------ #

_NUMBERS = {"floor": 80, "cap": 600}


def _fill(template: str) -> str:
    """An agent prompt with every field filled: text as the sentinel (her
    words, the pack's, the transcript), lengths as numbers."""
    names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    return template.format(**{n: _NUMBERS.get(n, SENTINEL) for n in names})


async def agent_keeper_task(rig: Rig) -> str:
    return _fill(ek._KEEPER_TASK)


async def agent_full_skeptic_task(rig: Rig) -> str:
    return _fill(ek._FULL_SKEPTIC_TASK)


async def agent_mode_b_skeptic_task(rig: Rig) -> str:
    return _fill(evolution_trigger._SKEPTIC_TASK)


async def agent_consolidation_task(rig: Rig) -> str:
    return _fill(consolidation._KEEPER_TASK)


async def agent_keeper_retry(rig: Rig) -> str:
    """The keeper's retry task: its first candidate restated the pack, so the
    retry carries the feedback quoting that sentence (and, with no current
    self yet, the first-version placeholder)."""
    factory = SENTINEL * 4
    cfg = rig.kernel.settings.evolution
    floor, cap = cfg.current_self_min_chars, cfg.current_self_max_chars
    tasks: list[str] = []

    async def fake_keeper(**kw):
        tasks.append(kw["task"])
        if len(tasks) == 1:
            return {"details": f"CANDIDATE\n{'x' * floor}\n{factory}"}
        return {"details": "NO_CHANGE"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ek, "_run_keeper_agent", fake_keeper)
        out = await ek.run_evolution_pass(
            model=None, renderer=None, memsearch=None, memory_root=rig.memory_root,
            transcripts_root=rig.ctx.transcripts_root, tool_output_store=None,
            pack_identity=Identity(self=factory, personality=factory, taboos=SENTINEL),
            base_persona=SENTINEL,
            enforcement=rig.kernel._doll_pack.enforcement, floor=floor, cap=cap,
            max_tokens=2048, now=rig.now,
        )
    assert out == "no_change" and len(tasks) == 2
    return tasks[1]


async def agent_keeper_evidence_bundle(rig: Rig) -> str:
    """The keeper's evidence block over files her tools wrote, plus a
    consolidated day (where the keeper reads it: ``memory_root/consolidated``)."""
    await _write_her_files(rig)
    today = f"{date.today():%Y-%m-%d}"
    consolidated = rig.memory_root / "consolidated" / f"{today}.md"
    consolidated.parent.mkdir(parents=True, exist_ok=True)
    consolidated.write_text(f"- {SENTINEL}\n", encoding="utf-8")
    bundle, _ = ek.assemble_bundle(memory_root=rig.memory_root, hwm=0,
                                   window_days=28.0, now=rig.now)
    # Every class is in it: the diary and the consolidated day by date label,
    # the self_history events with the external-content pin.
    assert len(re.findall(rf"^\[[^\]\n]+ {today}\]$", bundle, re.M)) == 2
    assert "external_ctx=true" in bundle
    return bundle


# ---- ch: turns ---------------------------------------------------------------- #


def _channel_delta(rig: Rig, *messages: dict) -> str:
    """A ch: turn's message for ChannelMessages in ``ch-1``, in the tier
    MindLoop derives for them, on a page whose snapshot is current."""
    perceptions = [Perception(kind="ChannelMessage", t=rig.now,
                              data={"channel_id": "ch-1", "content": SENTINEL, **m})
                   for m in messages]
    tier = rig.ml._derive_origin_tier(perceptions)
    seen = Seen()
    render_epoch_start(rig.state, seen, tier, rig.now, channel_id="ch-1")
    return render_turn_delta(rig.state, seen, tier, perceptions, rig.now)


async def channel_owner_dm(rig: Rig) -> str:
    return _channel_delta(rig, {"author": "owner", "author_is_owner": True, "is_dm": True})


async def channel_owner_in_channel(rig: Rig) -> str:
    return _channel_delta(rig, {"author": "owner", "author_is_owner": True, "is_dm": False,
                                "channel": "general"})


async def channel_stranger(rig: Rig) -> str:
    return _channel_delta(
        rig,
        {"author": "alice", "author_is_owner": False, "is_dm": True},
        {"author": "bob", "author_is_owner": False, "is_dm": False, "channel": "general"},
    )


_PEER = {"author": "peer-bot", "author_is_owner": False, "is_dm": True, "channel_kind": "mcp"}


async def channel_ai_peer(rig: Rig) -> str:
    return _channel_delta(rig, _PEER)


async def channel_ai_peer_debug(rig: Rig) -> str:
    return _channel_delta(rig, {**_PEER, "debug_reliable": True})


async def snapshot_external_public(rig: Rig) -> str:
    """A public channel's new page (carries [External situation])."""
    return render_epoch_start(rig.state, Seen(), "external_public", rig.now,
                              channel_id="ch-1")


# --------------------------------------------------------------------------- #
# The guard                                                                    #
# --------------------------------------------------------------------------- #


CASES = [
    system_prompt,
    system_prompt_with_current_self,
    tools_internal,
    tools_external_public,
    snapshot_default_state,
    snapshot_filled_state,
    delta_awoke,
    delta_user_spoke,
    delta_bootstrap_schedule,
    delta_reflection,
    evolution_block_other_origins,
    delta_persona_drift,
    delta_bridge_and_mcp_down,
    delta_pulse,
    delta_tool_result,
    delta_other_internal_kinds,
    delta_tool_habits,
    delta_memory_hits_with_provenance,
    diary_request,
    tool_result_unknown_tool,
    tool_result_bad_arguments,
    tool_result_dispatched,
    tool_result_invoke_skill_missing,
    tool_result_recall_provenance,
    tool_result_pin_self,
    tool_result_self_revision,
    tool_result_learn_name,
    memory_transcript_and_action_log,
    memory_her_files,
    agent_keeper_task,
    agent_full_skeptic_task,
    agent_mode_b_skeptic_task,
    agent_consolidation_task,
    agent_keeper_retry,
    agent_keeper_evidence_bundle,
    channel_owner_dm,
    channel_owner_in_channel,
    channel_stranger,
    channel_ai_peer,
    channel_ai_peer_debug,
    snapshot_external_public,
]


@pytest.mark.parametrize("source", CASES, ids=lambda fn: fn.__name__)
async def test_private_prompt_has_only_her_own_chinese(rig: Rig, source) -> None:
    text = await source(rig)
    assert text.strip()
    assert_only_allowed(text, ALLOWED)
