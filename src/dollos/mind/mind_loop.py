"""MindLoop — the single persistent coroutine that IS Doll's consciousness."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import aclosing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dollos.cascade.cascade_ctx import CascadeCtx
from dollos.cascade.sentence_chunker import SentenceChunker
from dollos.cascade.tool_loop import ToolResult, dispatch_one
from dollos.character import Enforcement
from dollos.ipc.messages import AddressedText, TextChunk
from dollos.llm.templates import build_voice_first_grammar
from dollos.memory_writer import append_transcript
from dollos.mind.associative_search import associative_search
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_prompt import OPEN_LOOP_STALE_S, energy_bucket_line, render_mind
from dollos.mind.mind_state import (
    MindState,
    OutputRecord,
    Perception,
    save_state,
)
from dollos.mind.perception_queue import PerceptionQueue
from dollos.mind.persona_guard import check_persona_violations
from dollos.mind.repeat_detect import detect_repeat_streak
from dollos.mind.tool_memory import record_tool_outcome, render_tool_outcomes, tool_habits_search
from dollos.stream_events import SpeakChunk, ToolCallReady
from dollos.tool_parser import ToolStreamParser
from dollos.tools import NoteToolLesson, PinSelf, SelfRevision
from dollos.wal.perception_log import PerceptionWAL

logger = logging.getLogger(__name__)

# Budget cap on the in-turn sync-tool re-feed cascade (spec §8.2). Generous
# enough for legitimate multi-step plans, but a rotating-invalid-call storm or a
# sync-tool loop terminates deterministically. This is a convergence criterion,
# NOT a fallback.
MAX_SYNC_REFEED_PASSES = 8

# Fire-and-forget tools spawn a background worker and re-enter as perceptions
# across turns BY DESIGN (spec §Gap A) — their dispatch ack string must NOT
# trigger an in-turn re-feed pass. Classified by name here because the live loop
# owns the cascade-worthiness decision; `tools.py` is untouched by P1.
FIRE_AND_FORGET_TOOLS = frozenset({"Shell", "SpawnWorkflow", "SpawnMonitor"})

# In-turn re-feed allowlist (spec §7 P1). On SUCCESS, only a tool whose RESULT
# Doll genuinely must read to decide what to do next warrants another full
# streaming decode pass. `Recall` returns memory hits that change her next
# decision. `PinSelf` is included for a different reason: `self_profile.apply`
# reports its SOFT failures (replace/remove target-not-found, over-cap) as
# plain strings, and `dispatch_one` marks any non-None string return as
# success=True — so those soft-failures look like successes to the dispatcher.
# The refeed allowlist is what lets Doll see the "找不到…" / cap message and
# retry a failed pin in the SAME turn (A1 b9d87b7). `NoteMemory` stays excluded
# — it's a pure confirmation that never needs a retry. Re-feeding a genuinely
# successful `add` risks the weak model re-emitting an identical pin next pass;
# that's defanged by idempotent add-dedup in `self_profile.apply` (see
# self_profile.py), not by excluding PinSelf here.
# Tool FAILURES of ANY tool still re-feed (external grounding so Doll is told
# and can fix her mistake) — this allowlist gates SUCCESS only.
# `SelfRevision` (spec §3.4) is included for the same reason as `PinSelf`: its
# success message (e.g. counter「已送審」/friendly refusal) is content Doll must
# read to know what happened; a SECOND mutation the same turn is defanged by
# the per-turn `evolution_latched` latch, not by exclusion here.
IN_TURN_REFEED_TOOLS = frozenset({"Recall", "PinSelf", "SelfRevision"})

# Perception kinds that mean "this turn's context contains externally-sourced
# content" (spec 2026-07-02 §3.2). [Memory context] auto-injection deliberately
# does NOT count — it is present on almost every turn and would saturate the flag.
# P1e spec §3.4 S1: ChannelMessage (Discord) added — a Discord-originated turn
# now down-weights PinSelf writes via the existing self_history provenance path,
# same as a tool-result/monitor turn.
_EXTERNAL_KINDS = frozenset({"ToolResultArrived", "MonitorFired", "MonitorEnded", "ChannelMessage"})


def batch_external(perceptions) -> bool:
    """True when the drained batch carries external content (provenance flag)."""
    return any(p.kind in _EXTERNAL_KINDS for p in perceptions)

# Read-only safe mode (spec §8.3). After this many CONSECUTIVE tool failures
# within a single live turn — OR the same-tool 3-strike stuck flag — Doll
# narrows to a read-only tool set and announces it. K=3 catches the
# rotating-invalid-call storm the same-tool 3-strike misses. Bounded-severity,
# announced boundary — NOT a fallback.
SAFE_MODE_FAIL_THRESHOLD = 3

# The only tools available while safe_mode is set: read-only / reversible reads
# plus naked-text speech (always available, not a tool). Everything that writes
# memory/diary/schedule/scratchpad or spawns an external action is excluded so a
# failure storm cannot do irreversible damage while the loop is wedged.
SAFE_MODE_TOOLS = frozenset({"Recall", "ReadToolOutput", "GrepToolOutput"})


# P1f trace (spec 2026-07-03 §T-C2): serialize the dataclass sources 1:1 with
# MindState.to_dict's asdict() usage — no invented fields, no divergence.
def _mood_to_dict(mood) -> dict:
    return asdict(mood)


def _open_loop_to_dict(loop) -> dict:
    return asdict(loop)


def _output_to_dict(output) -> dict:
    return asdict(output)


class MindLoop:
    """The single coroutine that runs Doll's consciousness.

    Lifecycle: spawned once by Kernel at daemon startup. Runs forever
    until shutdown signaled. Each iteration:
      1. Drain perceptions from queue (blocks until at least one arrives)
      2. Auto-sync external state (process_registry → active_tasks, etc.)
      3. Render full MindState as prompt
      4. Call LLM once
      5. Parse 0..N actions
      6. Execute actions (sync inline or async dispatch)
      7. Persist state
    """

    def __init__(
        self,
        *,
        state: MindState,
        queue: PerceptionQueue,
        ctx: MindCtx,
        llm: Any,                    # LLM client with stream_completion API
        system_prompt: str,          # rendered from character pack
        state_persist_path: Path,
        tool_registry: dict[str, type[BaseModel]] | None = None,
        system_pulse: Any = None,
        cognition: Any = None,
        wal: PerceptionWAL | None = None,
        primary_language: str = "繁體中文",
        cascade_logger=None,
        energy_enabled: bool = False,
        cost_per_turn: float = 0.05,
        self_profile_enabled: bool = False,
        enforcement: Enforcement | None = None,
        system_prompt_suffix: str = "",
        evolution_enabled: bool = False,
        current_self_min_chars: int = 80,
        current_self_max_chars: int = 600,
        pending_max_surfacings: int = 5,
        pending_min_age_days: float = 2.0,
        trace_writer=None,
        model_id: str | None = None,
    ) -> None:
        self._state = state
        self._queue = queue
        self._ctx = ctx
        self._llm = llm
        self._system_prompt = system_prompt
        self._system_prompt_suffix = system_prompt_suffix
        self._evolution_enabled = evolution_enabled
        self._current_self_min_chars = current_self_min_chars
        self._current_self_max_chars = current_self_max_chars
        self._pending_max_surfacings = pending_max_surfacings
        self._pending_min_age_days = pending_min_age_days
        # Content-keyed compose cache: (sanctioned_text_or_"", composed_prompt).
        # Recompose only when sanctioned text changes (weeks) — the prompt
        # cache stays warm (spec §3.1).
        self._composed_cache: tuple[str, str] | None = None
        self._primary_language = primary_language
        self._persist_path = state_persist_path
        self._tool_registry = tool_registry or {}
        self._system_pulse = system_pulse
        self._cognition = cognition
        self._wal = wal
        self._cascade_logger = cascade_logger
        self._trace_writer = trace_writer
        self._model_id = model_id
        self._shutdown = False
        self._cascade_ctx: CascadeCtx | None = None
        # Turn-local buffer of FULL spoken sentences (recent_outputs only keeps
        # a truncated summary, so transcript capture needs the complete text).
        self._turn_speech: list[str] = []
        # Lazily-built grammar for the reduced safe-mode tool set (spec §8.3).
        self._safe_grammar: str | None = None
        # Reflection-turn flag and lazily-built grammar for the expanded set.
        self._is_reflection: bool = False
        self._reflection_grammar: str | None = None
        # B3 energy system
        self._energy_enabled = energy_enabled
        self._cost_per_turn = cost_per_turn
        self._turn_had_tool: bool = False
        self._self_profile_enabled = self_profile_enabled
        # P8 mechanical persona enforcement (spec §2). Absent kwarg ⇒ empty
        # Enforcement() defaults ⇒ check_persona_violations always returns []
        # ⇒ zero behavior change for packs that don't opt in.
        self._enforcement = enforcement or Enforcement()

        # No-fallback (spec §3.3): a grammar build failure is a tool-set config
        # error. Let it raise at startup — the daemon must refuse to run with a
        # half-built / unconstrained tool set rather than silently degrade.
        if self._tool_registry:
            self._grammar = build_voice_first_grammar(
                list(self._tool_registry.values())
            )
        else:
            self._grammar = None

    def _system_prompt_for_turn(self) -> str:
        """Compose ``prefix ⊕ current_self_section ⊕ suffix`` for this turn,
        reading the SANCTIONED text (spec §3.1/§5). Content-keyed cache: the
        section changes only when the latest ``evo_adopt`` changes. Sanctioned
        text renders even when ``evolution.enabled`` is false — disabling
        evolution must not amputate an adopted self (spec §3.6)."""
        from dollos.mind import current_self, self_history
        sanctioned = self_history.sanctioned_text(
            self._ctx.memory_root / "self_history.jsonl"
        )
        key = sanctioned or ""
        if self._composed_cache is None or self._composed_cache[0] != key:
            section = current_self.render_section(sanctioned)
            composed = current_self.compose(
                self._system_prompt, section, self._system_prompt_suffix
            )
            self._composed_cache = (key, composed)
        return self._composed_cache[1]

    async def run(self) -> None:
        """Main loop. Runs until shutdown."""
        while not self._shutdown:
            try:
                await self.iterate()
            except Exception:
                logger.exception("MindLoop iteration crashed; continuing")

    async def iterate(self) -> None:
        """One iteration: drain → sync → render → llm → execute → persist,
        once per origin bucket (spec §3.1 R2-C1: drain_grouped partitions a
        mixed batch by channel so concurrent external conversations never
        cross-deliver into each other's sink or share one turn's context)."""
        buckets = await self._queue.drain_grouped()
        if not buckets:
            # drain_grouped() returned empty — shutdown signaled; skip this iteration
            return
        # Global max seq across ALL perceptions in ALL buckets, captured BEFORE
        # the loop (perceptions are consumed in-place). WAL truncation is
        # batch-final — one truncate through the whole drain's max seq AFTER
        # every bucket completes, mirroring the pre-refactor "truncate once per
        # drain" semantics. Per-bucket truncation would delete a later-arriving
        # other bucket's still-unprocessed perceptions (see _run_one_turn).
        batch_max_seq = max(
            (p.seq for bucket in buckets for p in bucket if p.seq is not None),
            default=None,
        )
        all_saved = True
        try:
            for bucket in buckets:
                self._ctx.current_origin = (bucket[0].data or {}).get("channel_id") or None
                if not await self._run_one_turn(bucket):
                    all_saved = False
        finally:
            self._ctx.current_origin = None

        # Truncate the WAL ONCE, through the whole drain's max seq — only when
        # EVERY bucket's state save durably succeeded. If any bucket's save
        # failed (all_saved False) OR any bucket raised (the exception
        # propagates past here before this runs), truncation is skipped so all
        # perceptions replay on next boot.
        if self._wal is not None and batch_max_seq is not None:
            if all_saved:
                self._wal.truncate_through(batch_max_seq)
            else:
                logger.warning(
                    "save failed in at least one bucket; skipping WAL "
                    "truncation to preserve perceptions for replay"
                )

    @staticmethod
    def _derive_origin_tier(perceptions: list[Perception]) -> str:
        """Per-turn origin tier (P1e spec §3.4 S1), computed once at drain from
        this bucket's perceptions: no ChannelMessage → "internal"; a
        ChannelMessage from the owner → "external_dm"; from anyone else →
        "external_public". P1a single-origin bucket, so a straight scan over
        this batch is authoritative — no cross-bucket bleed."""
        for p in perceptions:
            if p.kind == "ChannelMessage":
                return "external_dm" if p.data.get("author_is_owner") else "external_public"
        return "internal"

    async def _run_one_turn(self, perceptions: list[Perception]) -> bool:
        """Process one origin bucket: sync → render → llm → execute → persist.

        Extracted from ``iterate`` (P1a Task 4) so a mixed drain can run this
        body once per origin bucket instead of once per drain. Behavior is
        unchanged for the single-bucket case (origin=None → internal sink).

        Returns ``True`` when this bucket's ``save_state`` durably succeeded,
        ``False`` otherwise. The caller (``iterate``) aggregates these to decide
        whether the batch-final WAL truncation may run (all buckets must save).
        """
        for p in perceptions:
            self._state.recent_perceptions.append(p)
            if p.kind == "UserSpoke":
                self._state.last_user_at = p.t
                self._state.user_turn_count += 1
                user_text = p.data.get("text", "")
                if user_text:
                    try:
                        await append_transcript(
                            transcripts_root=self._ctx.transcripts_root,
                            memsearch=self._ctx.memsearch,
                            role="user",
                            text=user_text.replace("\n", " "),
                        )
                    except Exception:
                        logger.exception(
                            "transcript write (user) failed; continuing"
                        )

        # Gate NoteToolLesson to reflection turns only (Spec B §5).
        self._is_reflection = any(p.kind == "ReflectionMoment" for p in perceptions)

        # Evidence-layer provenance (spec §3.2): one turn value shared by all
        # cascade/refeed passes of this iteration.
        self._ctx.current_turn = self._state.iter_count
        self._ctx.external_ctx = batch_external(perceptions)
        self._ctx.origin_tier = self._derive_origin_tier(perceptions)

        # 慢變演化 per-turn latch reset (spec §3.4): a new perception batch
        # opens a fresh SelfRevision decision window. The surfaced-this-turn
        # gate (F5) resets alongside — SelfRevision refuses slot-mutating ops
        # until surface_or_expire re-arms it below.
        self._ctx.evolution_latched = False
        self._ctx.evolution_candidate_surfaced = False

        # Clear read-only safe mode at the start of a user turn (spec §8.3 exit).
        # The user is re-engaging, so full capability is restored for this turn;
        # if the turn fails again, safe mode re-triggers and re-announces.
        if self._state.safe_mode and any(p.kind == "UserSpoke" for p in perceptions):
            logger.info("user turn — clearing read-only safe mode")
            self._state.safe_mode = False
            self._state.safe_mode_reason = ""

        # Auto-sync external state into MindState
        # TODO Task 8.5: ProcessRegistry → state.active_tasks
        # TODO Task 8.5: Schedule → state.pending_events

        # Memsearch query from recent perceptions
        memsearch_hits = await self._derive_memory_hits()

        # Context-associative recall (additive side-channel)
        try:
            associative_hits = await associative_search(
                self._ctx.memsearch, self._state, top_k=3
            )
        except Exception:
            logger.exception("associative_search failed; continuing without")
            associative_hits = []

        # Tool-habits retrieval (additive side-channel)
        try:
            tool_habits_hits = await tool_habits_search(
                self._ctx.memsearch, self._state,
                self._ctx.memory_root / "shared" / "tool_playbook.md",
            )
        except Exception:
            logger.exception("tool_habits_search failed; continuing without")
            tool_habits_hits = []

        # Pull self-pulse snapshot (None when no bucket shift since last emit)
        pulse_block: str | None = None
        if self._system_pulse is not None:
            try:
                pulse_block = self._system_pulse.snapshot()
            except Exception:
                logger.exception("system_pulse.snapshot raised; omitting block")

        # Pull cognition snapshot (None when no mind-state shift)
        cognition_block: str | None = None
        if self._cognition is not None:
            try:
                cognition_block = self._cognition.snapshot()
            except Exception:
                logger.exception("cognition.snapshot raised; omitting block")

        # The try/finally here wraps the render calls too so the turn-end None
        # sentinel is always emitted — even when render_tool_outcomes() or
        # render_mind() raises (I1: previously those could leave IPC clients
        # blocked waiting for a TurnEnd that never arrived).
        self._turn_speech.clear()
        self._turn_had_tool = False
        try:
            # 慢變演化 tamper tripwire (spec §5): detect/repair external edits
            # before rendering. Frozen when evolution disabled (already-sanctioned
            # text still renders via _system_prompt_for_turn). Deliberately
            # UNWRAPPED (spec §3.2: the IO-swallow rule is pins-only — evolution
            # events are never swallowed). An OSError here aborts the turn via the
            # finally below (which still emits the turn-end sentinel so IPC
            # clients never hang — I1), then run()'s outer catch retries next turn;
            # every process_tripwire branch is log-before-mutate, so an aborted
            # turn leaves the file divergent and the next turn re-classifies.
            # MUST run inside this try (F2): outside it, an OSError would skip the
            # turn-end sentinel and hang clients.
            if self._evolution_enabled:
                from dollos.mind import evolution as _evo
                _evo.process_tripwire(
                    current_self_path=self._ctx.memory_root / "current_self.md",
                    history_path=self._ctx.memory_root / "self_history.jsonl",
                    slot_path=self._ctx.memory_root / "self_evolution" / "pending.json",
                    enforcement=self._enforcement,
                    floor=self._current_self_min_chars,
                    cap=self._current_self_max_chars,
                    now=time.time(),  # module-level `time` (line 5) — no local shadow
                )

            # Pre-render [Tool outcomes] block for reflection turns (Spec B Task 6).
            tool_outcomes_block = None
            if self._is_reflection:
                tool_outcomes_block = render_tool_outcomes(
                    self._state.tool_stats, self._state.recent_tool_failures
                )

            # Render prompt
            energy_line = (
                energy_bucket_line(self._state.energy)
                if self._energy_enabled
                else None
            )
            self_profile_text = None
            if self._self_profile_enabled:
                from dollos.mind import self_profile as _sp
                self_profile_text = _sp.render_block(
                    self._ctx.memory_root / "self_profile.md"
                )
            evolution_block = None
            if (self._evolution_enabled and self._is_reflection
                    and not self._state.safe_mode):
                from dollos.mind import evolution as _evo
                from dollos.mind import self_history as _sh
                hist_path = self._ctx.memory_root / "self_history.jsonl"
                evolution_block = _evo.surface_or_expire(
                    slot_path=self._ctx.memory_root / "self_evolution" / "pending.json",
                    history_path=hist_path,
                    current_self_path=self._ctx.memory_root / "current_self.md",
                    sanctioned_text=_sh.sanctioned_text(hist_path),
                    max_surfacings=self._pending_max_surfacings,
                    min_age_days=self._pending_min_age_days,
                    now=time.time(),  # module-level `time` (line 5) — no local shadow needed
                    mind_state=self._state,
                )
                # Arm the surfaced-this-turn gate (F5) only when a candidate
                # actually reached Doll's eyes this turn. Refusals to adopt
                # unsurfaced text depend on this staying False otherwise.
                self._ctx.evolution_candidate_surfaced = evolution_block is not None
            prompt = render_mind(
                self._state,
                memsearch_hits,
                self._system_prompt_for_turn(),
                pulse_block=pulse_block,
                cognition_block=cognition_block,
                associative_hits=associative_hits,
                primary_language=self._primary_language,
                tool_outcomes_block=tool_outcomes_block,
                tool_habits_hits=tool_habits_hits,
                energy_line=energy_line,
                self_profile_text=self_profile_text,
                evolution_block=evolution_block,
            )

            # ── P1f trace: turn-level envelope 組裝(存實際內容非 hash;T-C2)──
            # 寫入失敗 loud 但不斷 turn(Global Constraint) — assembly itself can
            # raise (e.g. self_history read failure), so guard it the same way
            # cascade_logger.log_iter below is guarded: log + fall back to no
            # trace for this turn rather than aborting the turn.
            trace_blocks = None
            if self._trace_writer is not None:
                try:
                    import hashlib

                    from dollos.mind import self_history as _sh_trace

                    # identity 是 immutable+versioned pack(self._system_prompt,
                    # UNCOMPOSED — _system_prompt_for_turn() bakes in the mutable
                    # current_self section, which would defeat hashing a stable
                    # pack version) → hash 即可還原(比對 pack repo)。
                    # current_self 是 mutable(慢變演化 target)→ 必須存全文,否則
                    # 用舊 trace 還原會拿到錯身分(R2 current_self finding)。
                    identity_text = self._system_prompt
                    identity_hash = hashlib.sha256(
                        (identity_text or "").encode("utf-8")
                    ).hexdigest()
                    current_self_text = _sh_trace.sanctioned_text(
                        self._ctx.memory_root / "self_history.jsonl"
                    )
                    trace_blocks = {
                        "origin_channel": self._ctx.current_origin or "",
                        "situation": self._situation_tag(),
                        "model_id": self._model_id,
                        "perception_batch": [
                            {"kind": p.kind, "data": dict(p.data)} for p in perceptions
                        ],
                        "static_prefix": {
                            "identity_hash": identity_hash,
                            "current_self_text": current_self_text,
                            "situational_template_id": None,  # P1d
                        },
                        "dynamic_blocks": {
                            "memsearch_hits": memsearch_hits,
                            "associative_hits": associative_hits,
                            "tool_habits_hits": tool_habits_hits,
                            "situational_A_products": None,  # P1c/P1d A 充實管線
                            "mood": _mood_to_dict(self._state.mood),
                            "energy": self._state.energy,
                            "open_loops": [
                                _open_loop_to_dict(l) for l in self._state.open_loops
                            ],
                            "recent_perceptions": [
                                {"kind": p.kind, "data": dict(p.data)}
                                for p in self._state.recent_perceptions
                            ],
                            "recent_outputs": [
                                _output_to_dict(o) for o in self._state.recent_outputs
                            ],
                        },
                    }
                except Exception:
                    logger.exception(
                        "trace_blocks assembly failed; continuing without trace"
                    )
                    trace_blocks = None

            # Call LLM (streams text → sink; dispatches tool calls inline)
            await self._llm_iterate(prompt, trace_blocks=trace_blocks)
        finally:
            # Signal end-of-turn to the connection pump: a None turn-separator
            # is converted to TurnEnd by the IPC pump. Always fires once per
            # real turn (even on error) so a text/IPC client never hangs
            # waiting for turn_end. Voice path: TTSObservingSink passes None
            # through unchanged (not a TextChunk, so no TTS side effect).
            self._ctx.sink_resolver(self._ctx.current_origin).put_nowait(None)

        # B3 energy consumption — only when Doll produced cognitive output this turn.
        produced = bool(self._turn_speech) or self._turn_had_tool
        if self._energy_enabled and produced:
            self._state.energy = max(0.0, self._state.energy - self._cost_per_turn)

        # Doll-side transcript: one line per turn, full text (B1).
        doll_text = "".join(self._turn_speech).strip().replace("\n", " ")
        if doll_text:
            try:
                await append_transcript(
                    transcripts_root=self._ctx.transcripts_root,
                    memsearch=self._ctx.memsearch,
                    role="doll",
                    text=doll_text,
                )
            except Exception:
                logger.exception("transcript write (doll) failed; continuing")

            # P8 mechanical persona enforcement (spec §2): pure "announce",
            # never blocking — the transcript write above and everything
            # else proceed unchanged regardless of the outcome here.
            violations = check_persona_violations(doll_text, self._enforcement)
            if violations:
                self._queue.put(Perception(
                    kind="PersonaDriftDetected",
                    t=time.time(),
                    data={"violations": violations, "snippet": doll_text[:120]},
                ))

        # P6 deterministic successful-repeat detector (spec §13.1): checked
        # after this turn's tool executions have populated recent_outputs.
        # Unlike failure-triggered safe mode (§8.3) this does NOT narrow the
        # tool registry — a successful repeat means the tools work fine, so
        # the right intervention is a louder signal, not less capability.
        # Idempotent via last_repeat_alert_key: only enqueues when the streak
        # fingerprint changes (first trip, or an escalating count). If the
        # streak breaks, the field is deliberately left as-is — the next
        # genuine streak naturally produces a different (lower) count, so its
        # fingerprint differs and it re-alerts with no extra bookkeeping.
        streak = detect_repeat_streak(self._state.recent_outputs)
        if streak is not None:
            kind, summary, count = streak
            fingerprint = f"{kind}:{summary}:{count}"
            if fingerprint != self._state.last_repeat_alert_key:
                self._queue.put(Perception(
                    kind="RepeatLoopDetected",
                    t=time.time(),
                    data={"tool": kind, "summary": summary, "count": count},
                ))
                self._state.last_repeat_alert_key = fingerprint

        # Update counters + persist
        self._state.iter_count += 1
        self._state.last_iter_at = time.time()
        saved = save_state(self._state, self._persist_path)

        # WAL truncation is NOT done here — it is hoisted to iterate() and run
        # ONCE, batch-final, through the whole drain's global max seq (P1a Task 4
        # review fix). `truncate_through(seq)` removes ALL WAL entries with
        # seq <= arg GLOBALLY, and seq is assigned in global arrival order. With
        # drain_grouped splitting an interleaved window (A₁ seq1, B₁ seq2, A₂
        # seq3 → bucket A=[1,3], bucket B=[2]), truncating per-bucket through
        # bucket A's max seq (3) would delete B₁ (seq2) from the WAL BEFORE
        # bucket B runs — a crash before B completes would then lose B₁ forever.
        # So the caller truncates once, after every bucket, gated on all saves.
        return saved

    async def _derive_memory_hits(self) -> list[dict]:
        """Query memsearch from the most recent UserSpoke or last 3 perceptions."""
        query = ""
        for p in reversed(self._state.recent_perceptions):
            if p.kind == "UserSpoke":
                query = p.data.get("text", "")
                break
        if not query and len(self._state.recent_perceptions) > 0:
            # fallback: concat last 3 perception bodies
            last3 = list(self._state.recent_perceptions)[-3:]
            parts = [str(p.data) for p in last3]
            # P7 cheap slice (spec §13.2): also pull stale open-loop desc
            # text into the fallback query so idle/reflection/scheduled
            # turns actively retrieve memory related to unresolved
            # commitments, not just ambient recent-perception noise. Fresh
            # loops (age <= OPEN_LOOP_STALE_S) are excluded. Uses the exact
            # same strict `>` comparison as _render_open_loops.
            now = time.time()
            parts.extend(
                lp.desc for lp in self._state.open_loops
                if now - lp.opened_at > OPEN_LOOP_STALE_S
            )
            query = " ".join(parts)[:500]
        if not query:
            return []
        try:
            hits = await self._ctx.memsearch.search(query, top_k=10)
            # B2 candidate pull-only: consolidated/ sources are never auto-injected
            # into [Memory context] — they are only surfaced via Recall (with
            # provenance prefix). Gating is self-contained here, not delegated.
            hits = [h for h in hits if "consolidated/" not in (h.get("source") or "")]
            return hits
        except Exception:
            logger.exception("memsearch query failed; continuing with empty hits")
            return []

    def _situation_tag(self) -> str:
        """P1f 粗粒度 situation。P1d 情境渲染會細緻化(dm_owner/external_public/…),
        屆時靠 schema_version 遷移。"""
        if self._is_reflection:
            return "internal_reflection"
        if self._ctx.external_ctx:
            return "external"
        return "internal"

    def _active_tool_registry(self) -> dict[str, type[BaseModel]]:
        """The tool registry in force for this pass.

        safe_mode → read-only subset (SAFE_MODE_TOOLS); safe_mode has priority.
        reflection turn → full registry + NoteToolLesson, + PinSelf when
        self_profile_enabled (A1 self-profile; spec Task 5), + SelfRevision when
        evolution_enabled (慢變演化; spec §3.4). safe_mode's read-only subset is
        checked first, so SelfRevision (and PinSelf) never appear there —
        grammar-level suppression on safe-mode turns.
        otherwise → base registry.
        """
        if self._state.safe_mode:
            return {
                n: c for n, c in self._tool_registry.items()
                if n in SAFE_MODE_TOOLS
            }
        if self._is_reflection:
            extra = {"NoteToolLesson": NoteToolLesson}
            if self._self_profile_enabled:
                extra["PinSelf"] = PinSelf
            if self._evolution_enabled:
                extra["SelfRevision"] = SelfRevision
            return {**self._tool_registry, **extra}
        return self._tool_registry

    def _active_grammar(self) -> str | None:
        """Grammar for this pass, built from the active tool registry.

        safe_mode → lazily-built+cached reduced grammar (no-fallback: raises on
        build failure, never degrades to grammar=None; spec §8.3).
        reflection turn → lazily-built+cached expanded grammar (MAIN_TOOLS +
        NoteToolLesson, + PinSelf when self_profile_enabled, + SelfRevision
        when evolution_enabled — both static per-run predicates, so the cached
        grammar built on first reflection turn is correct for the whole run).
        otherwise → the once-built full grammar (no per-pass cost).
        """
        if self._state.safe_mode:
            if self._safe_grammar is None:
                self._safe_grammar = build_voice_first_grammar(
                    list(self._active_tool_registry().values())
                )
            return self._safe_grammar
        if self._is_reflection:
            if self._reflection_grammar is None:
                self._reflection_grammar = build_voice_first_grammar(
                    list(self._active_tool_registry().values())
                )
            return self._reflection_grammar
        return self._grammar

    def _enter_safe_mode(self, reason: str) -> None:
        """Enter read-only safe mode (idempotent within a turn).

        Sets the flag + reason, and enqueues a `SafeModeEntered` perception so
        Doll perceives the narrowing next turn and can ask the user for help.
        Announced + visible (banner) + grounded (perception) — not a silent
        degradation.
        """
        if self._state.safe_mode:
            return
        self._state.safe_mode = True
        self._state.safe_mode_reason = reason
        logger.warning("entering read-only safe mode: %s", reason)
        self._queue.put(Perception(
            kind="SafeModeEntered",
            t=time.time(),
            data={"reason": reason},
        ))

    @property
    def is_cascade_active(self) -> bool:
        """True iff _llm_iterate is currently running (a cascade is in flight)."""
        return self._cascade_ctx is not None

    def cancel_current_cascade(self) -> None:
        """Set cancel on the active cascade context, if any. No-op otherwise."""
        if self._cascade_ctx is not None:
            self._cascade_ctx.cancel()

    async def _llm_iterate(self, prompt: str, *, trace_blocks: dict | None = None) -> None:
        """Stream Doll's turn as an in-turn cascade (spec §7.1).

        Pass 1 streams the single rendered prompt; each subsequent pass re-feeds
        the SYNC inline tool results (Recall / NoteMemory) from the prior pass as
        ``<tool_response>`` user messages, so Doll can read-then-decide within the
        same turn. Async fire-and-forget tools (Shell / SpawnWorkflow /
        SpawnMonitor) do NOT extend the turn — they re-enter as perceptions across
        turns. A turn with no sync-tool result is exactly today's single pass.

        Each pass keeps the EXISTING voice machinery byte-for-byte
        (`ToolStreamParser(voice_mode=True)` + `SentenceChunker` + live sink), so
        speech still streams sentence-by-sentence to TTS with no TTFT regression.

        Cancellation spans the whole multi-pass turn: `self._cascade_ctx` is set
        once here and cleared in `finally`, and is honoured at every chunk /
        event / flush boundary AND at every pass boundary, so an external
        `cancel_current_cascade()` returns cleanly within ~one chunk window.

        Termination is deterministic (spec §8.2): a pass with no sync-tool result
        breaks; a same-tool 3-strike failure run aborts; and the pass count is
        hard-capped at `MAX_SYNC_REFEED_PASSES`.
        """
        sink = self._ctx.sink_resolver(self._ctx.current_origin)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        # Same-tool consecutive-failure tracker, spanning passes — ported from
        # `tool_loop.py` so the live loop and the subagent cascade share one
        # convergence rule.
        consecutive_fails: dict[str, int] = {}
        last_failed_tool: str | None = None
        # Generic consecutive-failure run across passes (any tool). Catches the
        # rotating-invalid-call storm the same-tool 3-strike above misses; trips
        # read-only safe mode at SAFE_MODE_FAIL_THRESHOLD (spec §8.3).
        consecutive_fail_count = 0
        # P1f trace: initialized OUTSIDE the try so `finally` can always see
        # it, even if an exception fires before begin_turn() runs.
        turn_trace = None

        try:
            self._cascade_ctx = CascadeCtx()
            turn_id = self._cascade_logger.start_turn() if self._cascade_logger is not None else None
            # Begin the turn envelope. Trace must NOT depend on cascade_logger
            # being wired — when turn_id is None (no logger), mint a
            # standalone id so the trace still gets a stable turn_id.
            if self._trace_writer is not None and trace_blocks is not None:
                try:
                    tid = turn_id or uuid.uuid4().hex[:8]
                    turn_trace = self._trace_writer.begin_turn(
                        turn_id=tid, ts=time.time(), **trace_blocks
                    )
                except Exception:
                    logger.exception("trace begin_turn failed; turn will have no trace")
                    turn_trace = None
            # P1f trace: byte-verbatim authority for `input_messages_delta`
            # (Task 4) — tracks how far into `messages` the previous pass's
            # snapshot reached, so each pass captures exactly what was newly
            # appended since then (assistant emit + filtered refeed
            # tool_responses), never a reconstruction from tool_calls/results.
            prev_msg_len = 0
            for pass_idx in range(MAX_SYNC_REFEED_PASSES):
                if self._cascade_ctx.cancelled:
                    logger.info("cascade cancelled at pass boundary; exiting cleanly")
                    return

                # Snapshot BEFORE this pass streams: at this point the prior
                # pass's tail-appends (assistant emit + refeed tool_responses)
                # are complete and this pass hasn't added anything yet, so
                # `messages[prev_msg_len:]` is exactly the delta this pass is
                # about to stream against. `dict(m)` shallow-copies each
                # message so later mutation of `messages` can't retroactively
                # alter the captured delta.
                input_messages_delta = [dict(m) for m in messages[prev_msg_len:]]
                prev_msg_len = len(messages)

                pass_start = time.monotonic()
                raw_buf, results, tool_calls = await self._stream_one_pass(
                    prompt=prompt,
                    messages=messages,
                    first_pass=(pass_idx == 0),
                    sink=sink,
                )
                pass_latency_ms = int((time.monotonic() - pass_start) * 1000)

                if self._cascade_ctx.cancelled:
                    return

                # Per-pass metacognition capture: extract the REVIEW think-line
                # and append it to the rolling self-review buffer. MOOD is parsed
                # too but deliberately NOT written to state.mood (spec §6.2 /
                # §WRONG.4) — only MoodTool may author mood.
                self._capture_review(raw_buf)

                if self._cascade_logger is not None:
                    try:
                        self._cascade_logger.log_iter(
                            turn_id=turn_id,
                            iter=pass_idx,
                            assistant_text="".join(raw_buf),
                            tool_calls=tool_calls,
                            results=results,
                            duration_ms=int((time.monotonic() - pass_start) * 1000),
                        )
                    except Exception:
                        logger.exception("cascade_logger.log_iter failed; continuing")

                # P1f trace: same-site, same-tuple capture as cascade_logger
                # above — both writers serialize from the identical
                # (raw_buf, results, tool_calls) locals so the two logs can
                # never drift apart. Trace stores raw/full content (T-C2):
                # raw_assistant_emit is the verbatim think+speech text (NOT
                # `_parse_think`'s 5-field extraction), and result `detail` is
                # NOT truncated to [:500] like cascade_log's copy is.
                # cancelled pass 在 _stream_one_pass 內即 return,不會到達這裡 —
                # 故被取消的 pass 既無 cascade_log 也無 trace pass(§3.6/§6.2(g) 明文取捨)。
                if turn_trace is not None:
                    try:
                        turn_trace.add_pass(
                            pass_idx=pass_idx,
                            input_messages_delta=input_messages_delta,
                            raw_assistant_emit="".join(raw_buf),
                            tool_calls=[
                                {"name": tc.get("name"), "args": tc.get("arguments")}
                                for tc in tool_calls
                            ],
                            results=[
                                {
                                    "tool_name": r.tool_name,
                                    "success": r.success,
                                    "detail": r.detail or "",
                                }
                                for r in results
                            ],
                            active_tools=sorted(self._active_tool_registry().keys()),
                            is_reflection=self._is_reflection,
                            safe_mode=self._state.safe_mode,
                            external=self._ctx.external_ctx,
                            latency_ms=pass_latency_ms,
                        )
                    except Exception:
                        logger.exception(
                            "trace add_pass failed; continuing (this pass omitted from trace)"
                        )

                # Record the assistant emit so the next pass sees the full
                # user → assistant(think+tool_call) → user(<tool_response>)
                # alternation.
                messages.append(
                    {"role": "assistant", "content": "".join(raw_buf)}
                )

                # Decide which results warrant another in-turn observe pass.
                # Fire-and-forget tools (dispatch ack only) never extend the
                # turn. Of the remaining SYNC results, a SUCCESS re-feeds only
                # if its tool is on the in-turn allowlist (Recall) — a
                # NoteMemory success returns a confirmation Doll need not read,
                # so re-feeding it would be a wasted decode on the latency path.
                # A FAILURE of any tool always re-feeds (external grounding so
                # Doll can correct), so failures cascade exactly as before.
                # Recall output is in-context external content from here on
                # (spec §3.2 — Recall never appears in the drained batch). Must
                # run over every executed result BEFORE the refeed decision
                # below, so a PinSelf emitted after a Recall in the same
                # cascade sees external_ctx=True.
                for r in results:
                    if r.tool_name == "Recall" and r.success:
                        self._ctx.external_ctx = True

                refeed = [
                    r for r in results
                    if r.tool_name not in FIRE_AND_FORGET_TOOLS
                    and (not r.success or r.tool_name in IN_TURN_REFEED_TOOLS)
                ]
                if not refeed:
                    break  # nothing new to observe → today's single pass

                # Same-tool 3-strike abort (ported from tool_loop.py:189-219)
                # plus the generic consecutive-failure run across passes.
                for r in refeed:
                    if r.success:
                        consecutive_fails.clear()
                        last_failed_tool = None
                        consecutive_fail_count = 0
                    else:
                        consecutive_fail_count += 1
                        if r.tool_name == last_failed_tool:
                            consecutive_fails[r.tool_name] = (
                                consecutive_fails.get(r.tool_name, 1) + 1
                            )
                        else:
                            last_failed_tool = r.tool_name
                            consecutive_fails = {r.tool_name: 1}
                stuck = next(
                    (n for n, c in consecutive_fails.items() if c >= 3),
                    None,
                )
                if stuck is not None or consecutive_fail_count >= SAFE_MODE_FAIL_THRESHOLD:
                    reason = (
                        f"same tool {stuck} failed 3x in a row"
                        if stuck is not None
                        else f"{consecutive_fail_count} consecutive tool failures"
                    )
                    logger.info(
                        "live cascade entering read-only safe mode (%s); "
                        "aborting re-feed", reason
                    )
                    self._enter_safe_mode(reason)
                    break

                if self._cascade_ctx.cancelled:
                    return

                # Re-feed each sync result as a <tool_response> user message
                # (external grounding — errors re-enter too, spec §7.3).
                for r in refeed:
                    detail = r.detail if r.detail else "(no output)"
                    messages.append({
                        "role": "user",
                        "content": f"<tool_response>\n{detail}\n</tool_response>",
                    })
            else:
                logger.info(
                    "live cascade hit MAX_SYNC_REFEED_PASSES (%d); stopping",
                    MAX_SYNC_REFEED_PASSES,
                )
        finally:
            if turn_trace is not None:
                # speech = this turn's full spoken text; silence = none spoken.
                speech = self._collect_turn_speech()
                turn_trace.finish(speech=speech, silence=(speech == ""))
            self._cascade_ctx = None

    async def _stream_one_pass(
        self, *, prompt: str, messages: list[dict], first_pass: bool, sink
    ) -> tuple[list[str], list[ToolResult], list[dict]]:
        """Stream ONE assistant pass through the voice_first parser.

        SpeakChunks → resolved sink + recent_outputs("Speech");
        ToolCallReady → dispatch the named tool inline (sequential), collecting
        each non-``None`` ``ToolResult`` so the outer cascade can decide whether
        to re-feed. Returns ``(raw_buf, results, tool_calls)``.

        Pass 1 uses `stream_completion(user=prompt)` (first-word latency
        identical to today); pass ≥ 2 uses `stream_messages(messages=…)` so the
        running conversation (incl. <tool_response>) is sent.

        Honours cancel from `self._cascade_ctx` at every chunk / event / flush
        boundary — on cancel it returns the partial buffers immediately.
        """
        parser = ToolStreamParser(voice_mode=True)
        chunker = SentenceChunker()
        raw_buf: list[str] = []
        results: list[ToolResult] = []
        tool_calls: list[dict] = []

        if first_pass:
            stream = self._llm.stream_completion(
                system="",
                user=prompt,
                prefill="",  # voice_first grammar emits </think> itself
                max_tokens=2048,
                grammar=self._active_grammar(),
                purpose="cascade",
            )
        else:
            stream = self._llm.stream_messages(
                system="",
                messages=messages,
                max_tokens=2048,
                grammar=self._active_grammar(),
                purpose="cascade",
            )

        # aclosing() ensures the SSE/HTTP connection is torn down on any early
        # exit (cascade cancel, exception) — not just on normal exhaustion.
        async with aclosing(stream) as astream:
            async for chunk in astream:
                if self._cascade_ctx.cancelled:
                    logger.info("cascade cancelled mid-stream; exiting cleanly")
                    return raw_buf, results, tool_calls
                if chunk.text:
                    raw_buf.append(chunk.text)
                    for event in parser.feed(chunk.text):
                        if self._cascade_ctx.cancelled:
                            logger.info(
                                "cascade cancelled before event dispatch; exiting"
                            )
                            return raw_buf, results, tool_calls
                        if isinstance(event, ToolCallReady):
                            tool_calls.append({"name": event.name, "arguments": event.arguments})
                        r = await self._handle_stream_event(event, sink, chunker)
                        if r is not None:
                            results.append(r)
                if chunk.done:
                    break

        if self._cascade_ctx.cancelled:
            return raw_buf, results, tool_calls

        for event in parser.flush():
            if self._cascade_ctx.cancelled:
                return raw_buf, results, tool_calls
            if isinstance(event, ToolCallReady):
                tool_calls.append({"name": event.name, "arguments": event.arguments})
            r = await self._handle_stream_event(event, sink, chunker)
            if r is not None:
                results.append(r)

        if not self._cascade_ctx.cancelled:
            # Flush the chunker tail — emit any remaining buffered text.
            self._flush_chunker(chunker, sink)

        return raw_buf, results, tool_calls

    def _collect_turn_speech(self) -> str:
        """Full spoken text for the turn currently in flight (P1f trace).

        Joins ``self._turn_speech`` — a turn-local buffer of FULL sentence
        text, cleared once per turn in `_run_one_turn` before `_llm_iterate`
        runs and appended to by `_handle_stream_event`/`_flush_chunker` as
        speech streams out (see the field comment at ``__init__``). Deliberately
        NOT derived from ``state.recent_outputs``: that deque's ``OutputRecord``
        only stores a ``summary`` truncated to 60 chars (``f"spoke: {sentence[:60]}"``)
        for prompt-rendering, and is additionally capped at ``maxlen=15`` — either
        property would silently clip a verbose turn's transcript, which is exactly
        the T-C2 "store content not hash/truncate" failure mode this trace exists
        to avoid. ``_turn_speech`` already holds the untruncated, turn-scoped text
        (it was added for the doll-side transcript writer for the same reason —
        see the ``B1`` full-text comment near its use in `_run_one_turn`).
        """
        return "".join(self._turn_speech)

    def _capture_review(self, raw_buf: list[str]) -> None:
        """Parse the accumulated raw emit and record any REVIEW line.

        Tolerant by construction: ``_parse_think`` returns a partial/empty dict
        when fields are missing, so a turn with no REVIEW appends nothing.
        """
        if not raw_buf:
            return
        from dollos.cascade_log import _parse_think

        fields = _parse_think("".join(raw_buf))
        review = fields.get("review")
        if review:
            self._state.recent_reviews.append(review)

    def _emit_sentence(self, sink, sentence: str) -> None:
        """Push one streamed sentence to `sink`, addressed to the current
        origin's channel when that origin is registered external (spec
        §3.1) — else plain TextChunk, unchanged (internal/unregistered
        origins, including the None origin of internal turns)."""
        origin = self._ctx.current_origin
        registry = self._ctx.channel_registry
        if origin and registry is not None and registry.locus_of(origin) == "external":
            sink.put_nowait(AddressedText(channel_id=origin, text=sentence))
        else:
            sink.put_nowait(TextChunk(text=sentence))

    def _flush_chunker(self, chunker: SentenceChunker, sink) -> None:
        for sentence in chunker.flush():
            if sentence:
                self._emit_sentence(sink, sentence)
                self._state.recent_outputs.append(OutputRecord(
                    kind="Speech",
                    t=time.time(),
                    summary=f"spoke: {sentence[:60]}",
                ))
                self._turn_speech.append(sentence)

    async def _handle_stream_event(
        self, event, sink, chunker
    ) -> ToolResult | None:
        """Handle one parser event.

        SpeakChunk → stream to sink (returns None). ToolCallReady → dispatch the
        named tool and return its ``ToolResult | None`` so the caller (the §P1
        re-feed loop, Task 7) can collect cascade-worthy results. A ``None``
        return means "nothing to observe this event" (speech, or a
        fire-and-forget tool that spawned a background worker).
        """
        if isinstance(event, SpeakChunk):
            if not event.text:
                return None
            for sentence in chunker.feed(event.text):
                self._emit_sentence(sink, sentence)
                self._state.recent_outputs.append(OutputRecord(
                    kind="Speech",
                    t=time.time(),
                    summary=f"spoke: {sentence[:60]}",
                ))
                self._turn_speech.append(sentence)
            return None
        elif isinstance(event, ToolCallReady):
            self._turn_had_tool = True
            return await self._dispatch_tool(event.name, event.arguments)
        return None

    async def _dispatch_tool(
        self, name: str, arguments: dict
    ) -> ToolResult | None:
        """Dispatch via shared dispatch_one (spec §3.6), then record the outcome
        into Doll's tool memory (Spec B Layer 1 — live-only)."""
        r = await dispatch_one(name, arguments, self._ctx, self._active_tool_registry())
        record_tool_outcome(self._ctx.mind_state, name, r)
        return r

    def shutdown(self) -> None:
        """Signal the loop to stop. Unblocks any pending drain()."""
        self._shutdown = True
        self._queue.shutdown()

