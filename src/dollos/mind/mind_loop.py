"""MindLoop — the single persistent coroutine that IS Doll's consciousness."""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dollos.cascade.cascade_ctx import CascadeCtx
from dollos.character import Enforcement
from dollos.harness.executor import ToolExecutor, ToolResult, dispatch_one, turn_label
from dollos.harness.permissions import ORIGIN_TIER, principal_of
from dollos.harness.registry import Offered, ToolRegistry
from dollos.harness.runner import PassRecord, Turn, TurnRunner
from dollos.harness.turn_policy import (
    MAX_SYNC_REFEED_PASSES,  # noqa: F401 — re-exported (tests import it from here)
    POLICIES,
    TurnKind,
    effective_kind,
    moment_of,
)
from dollos.ipc.messages import AddressedText, TextChunk, TurnEndAddressed
from dollos.llm.model import End, ModelAdapter
from dollos.memory_writer import append_transcript, append_action_log
from dollos.mind.action_log import action_phrase_for_tool, action_phrase_for_perception
from dollos.mind.associative_search import associative_search
from dollos.mind.context_builder import (
    PRIVATE,
    ContextBuilder,
    Conversation,
    conversation_key,
    one_off,
)
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_prompt import (
    OPEN_LOOP_STALE_S,
    energy_bucket_line,
    hits_in_context,
    render_body_signal,
    render_diary_request,
    render_epoch_start,
    render_memory_guideline,
    render_turn_delta,
)
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
from dollos.perception.system_pulse import thermal_multiplier
from dollos.telemetry.turn_latency import TurnLatencyRecord
from dollos.telemetry.vitals import VitalsRecord
from dollos.wal.perception_log import PerceptionWAL

logger = logging.getLogger(__name__)

# The turn's pass cap, MAX_SYNC_REFEED_PASSES, is TurnPolicy.max_passes
# (dollos.harness.turn_policy); its failure / refusal limits
# (SAFE_MODE_FAIL_THRESHOLD, UNBOUND_REJECT_LIMIT) live in
# dollos.harness.runner. Read-only safe mode (spec §8.3): the runner's
# safe-mode failure rule ends the turn and _llm_iterate enters it; its
# read-only tool set is POLICIES["safe_mode"].

# Per model call: the budget for speech and tool calls. The adapter adds the
# think budget ([llm].think_budget_tokens) on top.
MIND_MAX_TOKENS = 1024

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
      2. Append this turn's message to its place's conversation
         (``ContextBuilder``: only what changed since that conversation last
         saw it; a new page with a snapshot when needed)
      3. Run the tool loop on the model: stream, run the reply's tool calls,
         send the results back, repeat until a reply has no tool calls
      4. Persist state
    """

    def __init__(
        self,
        *,
        state: MindState,
        queue: PerceptionQueue,
        ctx: MindCtx,
        llm: ModelAdapter,
        system_prompt: str,          # rendered from character pack
        state_persist_path: Path,
        tool_registry: dict[str, type[BaseModel]] | None = None,
        system_pulse: Any = None,
        cognition: Any = None,
        wal: PerceptionWAL | None = None,
        primary_language: str = "English",
        cascade_logger=None,
        energy_enabled: bool = False,
        cost_per_turn: float = 0.05,
        token_per_energy_unit: float = 2000.0,
        thermal_multiplier_warm: float = 1.15,
        thermal_multiplier_hot: float = 1.4,
        self_profile_enabled: bool = False,
        enforcement: Enforcement | None = None,
        system_prompt_suffix: str = "",
        evolution_enabled: bool = False,
        current_self_min_chars: int = 250,
        current_self_max_chars: int = 2000,
        pending_max_surfacings: int = 5,
        pending_min_age_days: float = 2.0,
        trace_writer=None,
        model_id: str | None = None,
        on_turn_complete: Callable[[str | None, bool], None] | None = None,
        diary_max_log_chars: int = 40000,
        turn_latency_recorder=None,
        vitals_recorder=None,
        think_budget_tokens: int = 1024,
        conversation_budget_tokens: int = 24000,
    ) -> None:
        self._state = state
        self._queue = queue
        self._ctx = ctx
        self._llm = llm
        # [llm].think_budget_tokens: caps thinking on every model call.
        self._think_budget_tokens = think_budget_tokens
        # One append-only conversation per place (plan 2026-10-18 Task 9);
        # [llm].conversation_budget_tokens bounds a page.
        self._contexts = ContextBuilder(budget_tokens=conversation_budget_tokens)
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
        # Offered / bound sets over that registry (+ the kinds' extras).
        self._tools = ToolRegistry(self._tool_registry)
        self._system_pulse = system_pulse
        self._cognition = cognition
        self._wal = wal
        self._cascade_logger = cascade_logger
        self._trace_writer = trace_writer
        self._model_id = model_id
        # P1c Task 4: optional turn-complete hook, invoked with
        # (origin, spoke) once per completed turn (see _run_one_turn's tail).
        # Kernel wires this to AttentionGate.note_reply so the engagement
        # session's disengage counter only advances when Doll actually
        # speaks — kept as a plain callback (not an import) so MindLoop stays
        # flow-agnostic w.r.t. AttentionGate.
        self._on_turn_complete = on_turn_complete
        self._shutdown = False
        self._cascade_ctx: CascadeCtx | None = None
        # Turn-local buffer of FULL spoken sentences (recent_outputs only keeps
        # a truncated summary, so transcript capture needs the complete text).
        self._turn_speech: list[str] = []
        # This batch's turn kind (dollos.harness.turn_policy.moment_of, plan
        # 2026-10-27 Task 3): computed once per _run_one_turn from THIS
        # batch's perceptions, never re-derived later from stale/replayed
        # state. Safe mode is layered on top per pass (effective_kind) for the
        # tool binding only. With ctx.principal it decides the binding
        # (_active_tool_registry), speech suppression (_emit_sentence),
        # doll-transcript suppression, the kind's prompt blocks, and the
        # refusal label (_turn_kind).
        self._moment: TurnKind = "conversation"
        # Whole-branch review WB-1 (LOAD-BEARING, decoupled from the strict
        # pure-batch diary kind): "does THIS batch carry a DiaryMoment at
        # all", regardless of what else co-batched with it. The 23:00
        # DiaryMoment often co-batches with another origin-less internal
        # perception (AgendaMoment fires ~every 7 min, ReflectionMoment polls
        # ~every 5 s — both peak exactly when the system is idle at diary
        # time), which makes the strict diary kind not apply. Gating the I1
        # post-turn miss-check on the strict kind alone means a co-batched
        # diary turn that misses gets ZERO signal — no marker, no retry — the
        # exact silent failure I1 exists to prevent. This flag is used
        # SOLELY for that one post-turn check; every other diary-kind
        # consumer (registry narrowing, speech suppression, doll-transcript
        # suppression, [Today's log] injection) stays on the strict kind —
        # those must stay conservative so a co-batched live UserSpoke is
        # never silenced/whitewashed.
        self._diary_in_batch: bool = False
        # I1 per-day retry flag (spec §2.3): sentinel date-string, "" until
        # the first missed diary turn. Prevents an infinite re-enqueue loop —
        # only ONE retry per calendar day, regardless of how many more times
        # DiaryMoment fires and misses that same day.
        self._diary_retry_date: str = ""
        # [Today's log] safety ceiling (settings.diary.max_log_chars) — see
        # _read_today_log.
        self._diary_max_log_chars = diary_max_log_chars
        # B3 energy system
        self._energy_enabled = energy_enabled
        self._cost_per_turn = cost_per_turn
        self._turn_had_tool: bool = False
        self._turn_wrote_diary: bool = False
        # 代謝 vital (spec 2026-07-10 §2.2, Task 2): token-driven drain.
        self._token_per_energy_unit = token_per_energy_unit
        # 代謝 vital §2.3 (Task 5): hot-GPU multiplier on the token drain.
        self._thermal_mult_warm = thermal_multiplier_warm
        self._thermal_mult_hot = thermal_multiplier_hot
        # This turn's own token effort, accumulated across cascade passes via
        # on_usage (one call per pass — see _on_turn_usage). Contamination-
        # proof: only THIS loop's LLM calls hit THIS loop's callback. Cleared
        # per turn in the SAME spot as _turn_speech.clear() (_run_one_turn).
        self._turn_prompt_tokens: int | None = None
        self._turn_completion_tokens: int | None = None
        # Stashed at the drain site for Task 3's VitalsRecord.
        self._turn_energy_cost: float = 0.0
        self._turn_cost_mode: str = "measured"
        self._turn_tokens_total: int | None = None
        # Task 5: (gpu_hottest_c, gpu_power_w, battery_pct) from the SAME
        # latest_sample() the thermal multiplier reads, for VitalsRecord's
        # ambient fields. None until a real draining turn with a fresh
        # sample overwrites it (see the drain block below).
        self._turn_ambient: tuple[float | None, float | None, float | None] = (
            None, None, None,
        )
        # Task 3: mirror of `_llm_iterate`'s local `turn_id` join key. The
        # drain gate lives in `_run_one_turn`, AFTER `_llm_iterate` (and its
        # local `turn_id`) has already gone out of scope — so it's stashed
        # here on self instead. Set on every real turn (never gated on the
        # energy drain), so — unlike the three fields above, which are only
        # written inside the drain gate and can carry a stale prior-turn
        # value on a gate-skipped turn — this one is always fresh.
        self._turn_id: str | None = None
        # 延遲壓縮 Part 1 Task 2: turn-level think/speak/first-speak latency
        # telemetry (spec §3.2/§7). `turn_latency_recorder=None` is a full
        # no-op — every consumer below gates on it being non-None.
        self._turn_latency_recorder = turn_latency_recorder
        self._vitals_recorder = vitals_recorder
        self._turn_t0: float | None = None
        self._turn_first_speak_ms: float | None = None
        self._turn_think_chars: int = 0
        self._turn_speak_chars: int = 0
        self._turn_passes: int = 0
        # Whole-branch review Finding #1: this turn's triggering perception
        # kinds, deduped + sorted (e.g. ["UserSpoke"] vs ["AgendaMoment"]) —
        # stashed in `_run_one_turn` (the only place with the actual batch),
        # read back here in `_llm_iterate`'s `finally` for the
        # `TurnLatencyRecord`. Self-sufficient chat/non-chat discriminator
        # for Part 2 (spec §1.3/§3.2/§7.2).
        self._turn_perception_kinds: list[str] = []
        self._self_profile_enabled = self_profile_enabled
        # P8 mechanical persona enforcement (spec §2). Absent kwarg ⇒ empty
        # Enforcement() defaults ⇒ check_persona_violations always returns []
        # ⇒ zero behavior change for packs that don't opt in.
        self._enforcement = enforcement or Enforcement()

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

    def _system_for_turn(self) -> str:
        """The system message of every conversation: the composed prompt plus
        ``[Memory guideline]``. The same bytes all run long until current_self
        adopts a new version — with the tool list, the front of the server's
        cache prefix."""
        return (
            self._system_prompt_for_turn()
            + "\n\n"
            + render_memory_guideline(self._primary_language)
        )

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
        """Per-turn origin tier (P1e spec §3.4 S1), the compatibility view of
        ``principal_of`` (plan 2026-10-27 Task 2): no ChannelMessage →
        "internal"; a ChannelMessage from the owner AND in a DM (``is_dm``) →
        "external_dm"; anyone else, INCLUDING the owner posting in a PUBLIC
        channel → "external_public". Mixed ChannelMessages → the lowest-trust
        one (harness spec §6.5). P1a single-origin bucket, so a straight scan
        over this batch is authoritative — no cross-bucket bleed.

        Whole-branch review C1: ``author_is_owner`` alone used to be enough
        for "external_dm" (full private retrieval). But the owner can also
        post in a PUBLIC channel — the reply there is public, so it must not
        get the owner's full private-memory retrieval. ``is_dm`` (plumbed
        from the bridge, ``p.data["is_dm"]``) disambiguates: only
        owner-AND-DM is "external_dm"; owner-in-public degrades to
        "external_public" same as a stranger. A missing/falsy ``is_dm`` fails
        CLOSED (over-scopes to the safer tier), it never fails open.
        """
        return ORIGIN_TIER[principal_of(perceptions)]

    def _on_turn_usage(self, prompt: int | None, completion: int | None) -> None:
        """Accumulate this turn's own token usage (one call per cascade pass,
        via `on_usage` threaded through the LLM stream stack — Task 1). A
        None-usage pass contributes nothing; the turn total stays None only
        when NO pass reported usage (→ flat_legacy at the drain site)."""
        if prompt is not None:
            self._turn_prompt_tokens = (self._turn_prompt_tokens or 0) + prompt
        if completion is not None:
            self._turn_completion_tokens = (self._turn_completion_tokens or 0) + completion

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
            # P1e Task 5 (I4): an owner DM (ChannelMessage with
            # author_is_owner) is upgraded to UserSpoke-equivalent here —
            # it advances last_user_at/user_turn_count same as local chat,
            # so owner-present gating (e.g. energy restore pause) reacts to
            # owner DMs too. A stranger ChannelMessage (author_is_owner
            # falsy/absent) must NOT hit this branch.
            # Whole-branch review I2 (deliberate, NOT a bug): this PRESENCE
            # check stays keyed on raw ``author_is_owner`` alone — it does NOT
            # additionally require ``is_dm`` the way ``_derive_origin_tier``'s
            # "external_dm" classification now does (C1). The owner typing in
            # a PUBLIC channel is still the owner being present (cancels
            # consolidation/evolution, advances last_user_at) even though that
            # same turn's origin_tier is "external_public" for
            # CAPABILITY/PRIVACY purposes (tools/memory/retrieval/energy).
            # Presence and privacy are different axes — do not "fix" this to
            # require is_dm too.
            if p.kind == "UserSpoke" or (
                p.kind == "ChannelMessage" and p.data.get("author_is_owner")
            ):
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

            # World events (things that happened to her) → action log (Task
            # 4). Structurally owner/internal-only: strangers cannot produce
            # Shell/Workflow/Monitor results (those tools aren't in the
            # external_public TOOL_MATRIX row), so ToolResult/Monitor/
            # service-down perceptions never carry a stranger origin — no
            # origin gate needed here.
            # (Also: origin_tier is only derived AFTER this loop below, so it
            # would be stale if we tried to gate on it here.)
            wphrase = action_phrase_for_perception(p.kind, p.data or {})
            if wphrase:
                try:
                    await append_action_log(
                        transcripts_root=self._ctx.transcripts_root,
                        memsearch=self._ctx.memsearch,
                        phrase=wphrase,
                    )
                except Exception:
                    logger.exception("action-log write (event) failed; continuing")

        # This batch's kind (turn_policy.moment_of): reflection when any
        # ReflectionMoment is present; diary / agenda / pulse only when the
        # ENTIRE batch is that moment, so a moment co-batched with anything
        # else (e.g. a live UserSpoke) runs as a normal, non-narrowed,
        # non-suppressed turn — see moment_of for the whitewash rationale.
        self._moment = moment_of(perceptions)
        # WB-1: looser co-batch signal for the I1 post-turn miss-check ONLY
        # (see the `_diary_in_batch` field comment in `__init__`). Recomputed
        # every turn like `_moment` above — this assignment IS the reset.
        self._diary_in_batch = any(p.kind == "DiaryMoment" for p in perceptions)

        # Whole-branch review Finding #1: stash THIS batch's triggering
        # perception kinds for `_llm_iterate`'s `TurnLatencyRecord` (see the
        # `_turn_perception_kinds` field comment in `__init__`). Deduped +
        # sorted so the JSONL value is stable regardless of batch order.
        self._turn_perception_kinds = sorted({p.kind for p in perceptions})

        # Evidence-layer provenance (spec §3.2): one turn value shared by all
        # cascade/refeed passes of this iteration.
        self._ctx.current_turn = self._state.iter_count
        self._ctx.external_ctx = batch_external(perceptions)
        self._ctx.principal = principal_of(perceptions)
        self._ctx.origin_tier = ORIGIN_TIER[self._ctx.principal]
        # A new turn starts untainted (spec §6.5 point 2): the downgrade
        # lasts for the rest of ONE turn, never into the next.
        self._ctx.tainted = False

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

        # Memsearch query from this turn's own input. Every candidate goes to
        # the turn's message, which gives at most MEMORY_HITS_PER_TURN not yet
        # on the page (plan 2026-10-18 Task 8).
        memsearch_hits = await self._derive_memory_hits(perceptions)
        # Self-directed agenda (spec 2026-07-07 §3.1/§3.3): this turn's REAL
        # [Memory context] hit sources, read-only on ctx so PursueGoal can
        # auto-capture non-fabricable provenance from code, not model args.
        # Set once the message is rendered: exactly the hits in her context.
        self._ctx.turn_memory_sources = []

        # Context-associative recall (additive side-channel). P1e Task 4 (S3):
        # on an external_public turn this side-channel must not bypass the
        # same private-tier scoping Recall enforces — thread the scoping
        # rather than skip the whole call, so public turns still get
        # external_public associative hits.
        # Whole-branch review C2: switched from a denylist (exclude_prefixes=
        # private tiers) to a fail-closed ALLOWLIST (source_prefix=
        # external_public/ only). A denylist silently misses any tier not
        # enumerated in it (e.g. transcripts/ — owner+Doll verbatim convo —
        # was never in the private-tier list, so a stranger's associative
        # recall could pull it). The allowlist scopes to exactly the
        # external_public/ directory and nothing else.
        # Whole-branch verify (Important): the trailing separator makes this a
        # DIRECTORY boundary so it can't also match a sibling like
        # external_public_evil/ (LIKE '<prefix>%' with no boundary would) —
        # mirrors the exclude_prefixes '/%' boundary. See Recall.run.
        # S1 (harness spec §6.5): every other turn (internal, and external_dm
        # = the owner's DM) excludes external_public/ instead, so strangers'
        # notes never surface here unasked.
        is_public = self._ctx.origin_tier == "external_public"
        try:
            associative_hits = await associative_search(
                self._ctx.memsearch,
                self._state,
                top_k=3,
                source_prefix=(
                    str(self._ctx.memory_root / "external_public") + "/"
                    if is_public
                    else None
                ),
                exclude_prefixes=(
                    None if is_public
                    else [self._ctx.memory_root / "external_public"]
                ),
            )
        except Exception:
            logger.exception("associative_search failed; continuing without")
            associative_hits = []

        # Tool-habits retrieval (additive side-channel). P1e Task 4 (S3,
        # review fix): the tool playbook lives in the private tier
        # (shared/tool_playbook.md) and holds free-text lessons Doll wrote
        # about her tool use — potentially referencing the owner's activity.
        # Its hits render unconditionally into the [Tool habits] prompt block,
        # so on an external_public (stranger) turn this is a silent private
        # leak. Skip it entirely on public turns (mirrors the associative_search
        # gate above); the toolset is already conservative on such turns, so
        # tool-habits about Shell/Workflow are irrelevant there anyway.
        try:
            if self._ctx.origin_tier == "external_public":
                tool_habits_hits = []
            else:
                tool_habits_hits = await tool_habits_search(
                    self._ctx.memsearch, self._state,
                    self._ctx.memory_root / "shared" / "tool_playbook.md",
                )
        except Exception:
            logger.exception("tool_habits_search failed; continuing without")
            tool_habits_hits = []

        # The conversation of this bucket's place (plan 2026-10-18 Task 9).
        # [Self pulse]/[Cognition] are given once per change (snapshot()), so
        # only the private conversation reads them: a channel turn or the
        # diary consuming a change would hide it from private for good.
        key = conversation_key(self._ctx.current_origin)
        policy = POLICIES[self._moment]
        reads_body = key == PRIVATE and "today_log" not in policy.extra_blocks

        # [Body signal] framing (spec 2026-07-09 presence-tuning §3.2): only
        # on a pure PulseMoment turn (co-batched with anything else, this
        # stays None — same pure-batch guard as the pulse kind itself).
        body_signal_block: str | None = None
        if "body_signal" in policy.extra_blocks:
            wakes = [
                (
                    (p.data or {}).get("severity", "critical"),
                    (p.data or {}).get("detail", ""),
                )
                for p in perceptions
                if p.kind == "PulseMoment"
            ]
            body_signal_block = render_body_signal(wakes)

        # The try/finally here wraps the render calls too so the turn-end None
        # sentinel is always emitted — even when render_tool_outcomes() or
        # the message rendering raises (I1: previously those could leave IPC
        # clients blocked waiting for a TurnEnd that never arrived).
        self._turn_speech.clear()
        self._turn_had_tool = False
        self._turn_wrote_diary = False
        self._turn_prompt_tokens = None
        self._turn_completion_tokens = None
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
            if "tool_outcomes" in policy.extra_blocks:
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
                # Whole-branch review I1 (accepted P1e residual, NOT scoped):
                # self_profile.md renders on EVERY turn including
                # external_public, and MAY reference owner patterns Doll
                # noted about herself/the relationship. Scoping it out would
                # undermine "self always present" (Self-First design), and
                # the risk is lower than a raw-memory dump (curated, capped
                # content, not a search result) — accepted for P1e. Revisit
                # if this proves to leak in practice.
                from dollos.mind import self_profile as _sp
                self_profile_text = _sp.render_block(
                    self._ctx.memory_root / "self_profile.md"
                )
            evolution_block = None
            if (self._evolution_enabled
                    and "evolution_candidate" in policy.extra_blocks
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

            # The tier's fixed tool list and the system message: the
            # conversation's fixed prefix (a change opens a new page).
            offer = self._offer()
            offered, tools = offer.tools, offer.schemas
            system = self._system_for_turn()
            tier = self._ctx.origin_tier
            now = time.time()
            epoch_reason: str | None = None
            given_hits: list[dict] = []
            if "today_log" in policy.extra_blocks:
                # A pure diary turn is a one-off request (plan Task 9): this
                # moment's snapshot + [Today's log], never stored, private
                # untouched. Same system and tools, so the prefix resumes.
                conv = one_off(system, tools, render_diary_request(
                    self._state, perceptions, now,
                    today_log_block=self._read_today_log(),
                    self_profile_text=self_profile_text,
                    energy_line=energy_line,
                ))
            else:
                conv, epoch_reason = self._contexts.begin_turn(
                    key, system=system, tools=tools,
                    snapshot=lambda seen: render_epoch_start(
                        self._state, seen, tier, now,
                        channel_id=self._ctx.current_origin or "",
                        exclude=perceptions,
                        self_profile_text=self_profile_text,
                        energy_line=energy_line,
                        pulse_block=self._body_block(self._system_pulse, "current_block")
                        if reads_body else None,
                        cognition_block=self._body_block(self._cognition, "current_block")
                        if reads_body else None,
                    ),
                )
                # A new page's snapshot already carries the current body.
                reads_change = reads_body and epoch_reason is None
                conv.messages.append({"role": "user", "content": render_turn_delta(
                    self._state, conv.seen, tier, perceptions, now,
                    self_profile_text=self_profile_text,
                    energy_line=energy_line,
                    memory_hits=memsearch_hits,
                    associative_hits=associative_hits,
                    tool_habits_hits=tool_habits_hits,
                    pulse_block=self._body_block(self._system_pulse, "snapshot")
                    if reads_change else None,
                    cognition_block=self._body_block(self._cognition, "snapshot")
                    if reads_change else None,
                    tool_outcomes_block=tool_outcomes_block,
                    evolution_block=evolution_block,
                    body_signal_block=body_signal_block,
                )})
                given_hits = hits_in_context(memsearch_hits, conv.seen)
            self._ctx.turn_memory_sources = [
                h.get("source", "") for h in given_hits if h.get("source")
            ]

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
                        "conversation": conv.key,
                        "epoch": conv.epoch if conv.key is not None else None,
                        # The page opened this turn: enough, with each pass's
                        # input_messages_delta, to rebuild every request.
                        "epoch_start": {
                            "reason": epoch_reason,
                            "tools_hash": conv.tools_hash,
                            "system_hash": conv.system_hash,
                            "snapshot": conv.messages[1]["content"],
                        } if epoch_reason is not None else None,
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
                            "memsearch_hits": given_hits,
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
            await self._llm_iterate(
                conv, tools=tools, offered=offered, trace_blocks=trace_blocks,
            )
        finally:
            # Signal end-of-turn to the connection pump. For an EXTERNAL origin,
            # emit a channel-addressed turn-end (TurnEndAddressed) so a
            # multiplexing connector (mcp/discord) can tell which channel_id
            # finished — symmetric to _emit_sentence's AddressedText branch.
            # For internal/voice (origin-less) turns, emit the None separator
            # exactly as before (pump converts it to a global TurnEnd; the
            # voice TTSObservingSink passes None through unchanged). Always
            # fires once per real turn (even on error) so no client hangs.
            origin = self._ctx.current_origin
            registry = self._ctx.channel_registry
            if origin and registry is not None and registry.locus_of(origin) == "external":
                self._ctx.sink_resolver(origin).put_nowait(
                    TurnEndAddressed(channel_id=origin)
                )
            else:
                self._ctx.sink_resolver(self._ctx.current_origin).put_nowait(None)

        # B3 energy consumption — only when Doll produced cognitive output this turn.
        # P1e Task 5 (I4): a stranger's Discord turn (external_public) is exempt —
        # random public chatter must not drain the energy budget that gates her
        # sleep-consolidation cycle. internal and external_dm (owner, upgraded
        # below) both still consume, same as before this task.
        produced = bool(self._turn_speech) or self._turn_had_tool
        consumes = self._ctx.origin_tier != "external_public"
        if self._energy_enabled and produced and consumes:
            p = self._turn_prompt_tokens
            c = self._turn_completion_tokens
            if p is not None or c is not None:                    # measured
                token_cost = ((c or 0) + 0.25 * (p or 0)) / self._token_per_energy_unit
                cost_mode = "measured"
                tokens_total = (c or 0) + (p or 0)
            else:                                                  # D1 = sanctioned degrade
                token_cost = self._cost_per_turn
                cost_mode = "flat_legacy"
                tokens_total = None
            # 代謝 vital §2.3 (Task 5): hot GPU makes the SAME token effort
            # cost more ATP. latest_sample() is read ONCE here and shared by
            # both the multiplier and the VitalsRecord ambient fields below —
            # never re-queried, so the two can't observe different samples.
            # No fallback: a stale/absent sample (latest_sample()'s own
            # >2x-poll-interval staleness guard) leaves mult=1.0 and every
            # ambient field None, rather than fabricating a body reading.
            mult = 1.0
            hottest_c = gpu_w = batt = None
            if self._system_pulse is not None:
                try:
                    s = self._system_pulse.latest_sample()
                except Exception:
                    s = None
                if s is not None:
                    if s.gpus:
                        hottest_c = max(t for _, t in s.gpus)
                        mult = thermal_multiplier(
                            hottest_c, self._thermal_mult_warm, self._thermal_mult_hot
                        )
                    if getattr(s, "gpu_power", None):
                        gpu_w = max(draw for draw, _ in s.gpu_power)
                    batt = s.battery_pct
            drain = token_cost * mult
            energy_before = self._state.energy
            self._state.energy = max(0.0, self._state.energy - drain)
            self._turn_energy_cost = drain
            self._turn_cost_mode = cost_mode
            self._turn_tokens_total = tokens_total
            self._turn_ambient = (hottest_c, gpu_w, batt)
            # 代謝 vital (spec 2026-07-10 §2.2, Task 3): per-turn RL substrate
            # = the (state, action, cost, state') tuple. MUST be emitted
            # INSIDE this gate, not unconditionally at end of turn — outside
            # it, a gate-skipped turn (e.g. external_public) would read this
            # turn's stash fields still holding the LAST draining turn's
            # values and silently emit a wrong vitals row for a turn that
            # never actually drained. `record()` itself never raises
            # (log-and-continue), and the dispatch here is also wrapped
            # defensively so a vitals failure can never break the turn.
            # energy_cost is NOMINAL effort (may exceed energy_before-energy_after
            # at the floor); energy_before/after give the actual delta.
            if self._vitals_recorder is not None:
                try:
                    await self._vitals_recorder.record(VitalsRecord(
                        ts=time.time(),
                        turn_id=self._turn_id,
                        tokens_total=self._turn_tokens_total,
                        energy_cost=self._turn_energy_cost,
                        energy_before=energy_before,
                        energy_after=self._state.energy,
                        cost_mode=self._turn_cost_mode,
                        gpu_hottest_c=self._turn_ambient[0],
                        gpu_power_w=self._turn_ambient[1],
                        battery_pct=self._turn_ambient[2],
                    ))
                except Exception:
                    logger.exception("vitals record dispatch failed; continuing")

        # Doll-side transcript: one line per turn, full text (B1). Suppressed
        # on a dedicated diary turn (spec §2.3, Task 7): her private
        # journaling commentary (if any leaked past _emit_sentence's own
        # suppression into _turn_speech — it still gets buffered there for
        # trace/audit) must not also land in the day's action-log transcript
        # that the NEXT diary turn reads back via _read_today_log.
        doll_text = "".join(self._turn_speech).strip().replace("\n", " ")
        if doll_text and POLICIES[self._moment].doll_transcript:
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

        # I1 no-fabrication post-turn guarantee (spec §2.3/§3, Task 7,
        # LOAD-BEARING): a dedicated diary turn that did NOT call WriteDiary
        # is a MISS, never silently backfilled. The only response is: warn
        # (operator-visible), leave an observable marker (trace/metrics
        # audit — recent_outputs, same surface every other turn event uses),
        # and re-enqueue exactly ONE retry DiaryMoment. `_diary_retry_date`
        # is the per-day flag that caps this at one retry — without it, a
        # model that keeps declining to write would re-enqueue forever.
        # Whole-branch review WB-1: gated on `_diary_in_batch` (co-batch-
        # tolerant), NOT the strict diary kind — a DiaryMoment riding along
        # with e.g. an AgendaMoment/ReflectionMoment must still get this
        # guarantee even though the turn itself ran non-narrowed (see the
        # `_diary_in_batch` field comment in `__init__`).
        if self._diary_in_batch and not self._turn_wrote_diary:
            today = date.today().isoformat()
            logger.warning(
                "diary turn ended with no WriteDiary (turn=%s)",
                self._state.iter_count,
            )
            self._state.recent_outputs.append(OutputRecord(
                t=time.time(),
                kind="DiaryMissed",
                summary=f"no diary @ {today}",
                origin_tier=self._ctx.origin_tier,
            ))
            if self._diary_retry_date != today:
                self._diary_retry_date = today
                self._queue.put(Perception(
                    kind="DiaryMoment", t=time.time(), data={},
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
        # Strangers' records are left out (harness §6.5): the alert is an
        # internal perception that quotes the summary on the owner's page,
        # so a stranger repeating a tool would put their text there. On a
        # stranger turn the filtered tail is unchanged, so nothing re-fires.
        streak = detect_repeat_streak(
            [o for o in self._state.recent_outputs if o.origin_tier != "external_public"]
        )
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

        # P1c Task 4: turn-complete hook. Fires only on this non-raising
        # path (an exception earlier in this turn skips it entirely — no
        # note_reply for a turn that never finished). `self._turn_speech` is
        # fully populated by now (nothing clears/appends to it again until
        # the next turn's top) and `self._ctx.current_origin` is still this
        # bucket's origin (iterate() only resets it to None after ALL
        # buckets in the drain finish). Guarded like every other optional
        # side-channel in this file (cascade_logger, trace_writer) so a
        # broken callback never breaks the turn.
        if self._on_turn_complete is not None:
            try:
                self._on_turn_complete(
                    self._ctx.current_origin, bool(self._turn_speech)
                )
            except Exception:
                logger.exception("on_turn_complete callback failed; continuing")

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

    async def _derive_memory_hits(self, perceptions: list[Perception]) -> list[dict]:
        """Query memsearch from this turn's own input: what was said in
        ``perceptions`` (UserSpoke text, ChannelMessage content), else their
        last 3 bodies. Returns every candidate, best first; the caller caps
        how many render.

        The query is this batch, not the newest UserSpoke in the global
        deque (mind-loop §10): an owner DM turn searches for the DM, a
        reflection turn for the reflection, not the last thing said locally.

        ``transcripts/`` is excluded at the SQL layer (plan 2026-10-18 Task
        8): a day's transcript is one chunk, and the owner's line is written
        before this query runs, so it matched almost every owner turn with
        the whole day. ``Recall`` still searches it.

        P1e Task 4 (S3): on an external_public (stranger) turn the auto-
        injected ``[Memory context]`` is suppressed entirely — not just
        scoped — so a stranger's turn never gets an unsolicited dump of
        Doll's memory. Explicit ``Recall`` is the only retrieval path on
        such turns, and it scopes out the private tier (tools.py).

        S1 (harness spec §6.5): on every other turn ``external_public/`` is
        excluded too, so a stranger's note never reaches the owner's or
        Doll's own turn unasked; ``Recall(include_external=True)`` is the
        only way to read it there.
        """
        if self._ctx.origin_tier == "external_public":
            return []
        said = []
        for p in perceptions:
            if p.kind == "UserSpoke":
                said.append(p.data.get("text", ""))
            elif p.kind == "ChannelMessage":
                said.append(p.data.get("content", ""))
        query = " ".join(t for t in said if t)
        if not query and perceptions:
            # fallback: concat this batch's last 3 perception bodies
            parts = [str(p.data) for p in perceptions[-3:]]
            # P7 cheap slice (spec §13.2): also pull stale open-loop desc
            # text into the fallback query so idle/reflection/scheduled
            # turns actively retrieve memory related to unresolved
            # commitments, not just ambient recent-perception noise. Fresh
            # loops (age <= OPEN_LOOP_STALE_S) are excluded.
            now = time.time()
            parts.extend(
                lp.desc for lp in self._state.open_loops
                if now - lp.opened_at > OPEN_LOOP_STALE_S
            )
            query = " ".join(parts)[:500]
        if not query:
            return []
        try:
            hits = await self._ctx.memsearch.search(
                query, top_k=10, exclude_prefixes=[
                    self._ctx.transcripts_root,
                    self._ctx.memory_root / "external_public",
                ],
            )
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
        if self._moment == "reflection":
            return "internal_reflection"
        if self._ctx.external_ctx:
            return "external"
        return "internal"

    def _read_today_log(self) -> str | None:
        """Read today's FULL action-log transcript for the dedicated diary
        turn (spec §2.2, Task 7).

        ``transcripts/{today}.md`` mixes ordinary user/doll conversation
        lines (written by ``append_transcript``) with ``▸``-marked action
        lines (written by ``append_action_log``, Tasks 1-4) — this is
        deliberately the WHOLE day's file, not a filtered/summarized slice,
        so the diary is grounded in everything that actually happened (I1:
        the only defense against fabrication is giving her the real log to
        write from). Capped at ``self._diary_max_log_chars`` (settings.diary.
        max_log_chars, default 40000 — usually the whole day fits) with a
        head+tail split so an outsized day still fits the context budget
        without losing the morning/evening bookends.

        Returns ``None`` when no log file exists for today (e.g. a
        DiaryMoment firing on a day with zero recorded activity), when the
        file exists but is empty/whitespace-only, or when the read fails for
        ANY reason (permissions, mid-write decode error, a directory sitting
        where the file should be, ...) — best-effort only. The caller passes
        that straight through to ``render_diary_request``'s
        ``today_log_block=None``, which simply omits the block. This is
        deliberately broad (``OSError``/``UnicodeError``, not just
        ``FileNotFoundError``): a
        read failure must never propagate out of turn composition and abort
        the diary turn BEFORE the I1 post-turn guarantee (warn + marker +
        one retry) runs — that would drop the turn with no signal at all.
        """
        cap = self._diary_max_log_chars
        f = self._ctx.transcripts_root / f"{date.today():%Y-%m-%d}.md"
        try:
            raw = f.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if not raw.strip():
            return None
        if len(raw) > cap:
            half = cap // 2
            raw = raw[:half] + "\n…(middle omitted)…\n" + raw[-half:]
        return "[Today's log] (your day today — material for your diary)\n" + raw

    @staticmethod
    def _body_block(provider, method: str) -> str | None:
        """``provider.method()`` — a ``[Self pulse]`` / ``[Cognition]`` block
        or None; a raising provider omits the block for this turn."""
        if provider is None:
            return None
        try:
            return getattr(provider, method)()
        except Exception:
            logger.exception("%s.%s raised; omitting block", type(provider).__name__, method)
            return None

    def register_mcp_tools(self, classes: Iterable[type[BaseModel]]) -> None:
        """Add the MCP tools listed at boot (``harness.mcp_client``; kernel
        calls this once, before the first turn, so the offered list is fixed
        for the run). Each is bound on conversation turns where its server's
        ``min_principal`` row allows the principal."""
        for cls in classes:
            self._tools.register_mcp(cls)

    def _active_tool_registry(self) -> dict[str, type[BaseModel]]:
        """The tool registry in force for this pass: this turn's kind (safe
        mode on top, ``effective_kind``) and principal looked up in
        ``turn_policy.bound_tools`` = the kind's tools ∩ the principal's
        ``TOOL_MATRIX`` row + the kind's extras. The per-kind rules and their
        safety rationale live in ``turn_policy.POLICIES``; the per-principal
        rows in ``permissions.TOOL_MATRIX``.

        The owner DM (principal owner_dm) is bound exactly like the owner at
        the local keyboard (user decision 2026-10-27, harness spec §6.3; the
        compromised-Discord-account risk was accepted by the user that day).
        Only its memory scoping (keyed on origin_tier) still differs."""
        return self._tools.bound(
            self._binding_kind(), self._ctx.principal,
            self_profile=self._self_profile_enabled,
            evolution=self._evolution_enabled,
        )

    def _binding_kind(self) -> TurnKind:
        """This pass's kind for the tools: safe mode on top of the batch's."""
        return effective_kind(self._moment, safe_mode=self._state.safe_mode)

    def _offered_tools(self) -> dict[str, type[BaseModel]]:
        """The tools the model is shown: one fixed list per origin tier
        (plan 2026-10-18 decision 3, amends harness §6.2/§6.4) — the union of
        what any turn kind of that tier can be bound to
        (``turn_policy.offered_tools``).

        It never depends on the turn kind or safe mode: the tool JSON sits at
        the very front of the prompt, so a list that changed with the turn
        kind (even just LearnName on an owner turn) would void the server's
        cache prefix. What this turn may actually run is still
        ``_active_tool_registry()``, enforced per call in ``_dispatch_bound``.

        internal and external_dm (owner DM = the owner at the keyboard, user
        decision 2026-10-27) → the base registry + NoteToolLesson + LearnName,
        + PinSelf when self_profile_enabled, + SelfRevision when
        evolution_enabled.
        external_public → its TOOL_MATRIX row (Recall, NoteMemory,
        WriteDiary). external_public never sees an internal tool's schema, so
        a stranger doesn't even learn Shell exists.
        """
        return self._offer().tools

    def _offer(self) -> Offered:
        """``_offered_tools`` with its tool JSON (``ToolRegistry.offered``)."""
        return self._tools.offered(
            self._ctx.principal,
            self_profile=self._self_profile_enabled,
            evolution=self._evolution_enabled,
        )

    def _turn_kind(self) -> str:
        """This turn's kind as named in the refusal of an offered-but-unbound
        tool: safe mode, then a stranger's turn, then the batch's kind (a
        conversation is "owner" when the owner is present)."""
        return turn_label(self._binding_kind(), self._ctx.principal)

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

    async def _llm_iterate(
        self,
        conv: Conversation,
        *,
        tools: list[dict],
        offered: dict[str, type[BaseModel]],
        trace_blocks: dict | None = None,
    ) -> None:
        """Run Doll's turn on the ``TurnRunner`` (harness §1, plan 2026-10-27
        Task 5) and apply what it did.

        The Turn: ``conv`` (this turn's message already appended), this
        tier's fixed tool list (``_offered_tools``), every call through
        ``_dispatch_bound`` (this turn's binding, plus the side effects only a
        live turn has: tool memory, the action log, the diary flag), speech
        sentence-chunked to the sink live (``_speak_sentence``, so TTS starts
        on the first sentence), and the interrupt handle. The runner's loop,
        its termination rules and its partial-reply handling are documented
        in ``dollos.harness.runner``; the policy is
        ``POLICIES[_binding_kind()]`` (at most ``MAX_SYNC_REFEED_PASSES``
        passes, the safe-mode failure rule). Fire-and-forget tools (Shell /
        SpawnWorkflow / SpawnMonitor) answer with a job receipt; their real
        result comes back later as a perception, the turn never waits for it.

        Cancellation spans the whole multi-pass turn: `self._cascade_ctx` is
        set once here and cleared in `finally`, so an external
        `cancel_current_cascade()` returns cleanly.

        Here, per pass: cascade_log, the trace, the latency counters, and
        Recall marking the context external. After the turn: a ``failed``
        safe-mode rule enters safe mode; ``brain_down`` raises its error,
        after a ``server`` error marks the conversation so the next turn
        opens a new page.
        """
        sink = self._ctx.sink_resolver(self._ctx.current_origin)
        # P1f trace: initialized OUTSIDE the try so `finally` can always see
        # it, even if an exception fires before begin_turn() runs.
        turn_trace = None
        # Whole-branch review Finding #1: same reasoning as `turn_trace`
        # above — initialized OUTSIDE the try so `finally`'s
        # `TurnLatencyRecord` construction can always read `turn_id`, even if
        # `self._cascade_logger.start_turn()` itself raises before assigning
        # it inside the try. Without this, that exception's `finally` would
        # hit `NameError: turn_id` and mask the original exception.
        turn_id = None
        # The first pass's End: TTFT / cache numbers for the TurnLatencyRecord.
        first_end: End | None = None
        # Task 3: reset the self-stashed mirror in lockstep with the local
        # default above — an exception before the mint below must not leave
        # `self._turn_id` carrying the PREVIOUS turn's id into this turn's
        # (would-be) VitalsRecord.
        self._turn_id = None

        # 延遲壓縮 Part 1 Task 2: turn epoch + reset turn-scoped latency
        # accumulators. Epoch is `_llm_iterate` entry (spec §3.2), NOT
        # `_run_one_turn` — this is the point Doll starts actually deciding
        # what to say, not perception/render bookkeeping. Reset here (not in
        # __init__ only) because `_llm_iterate` runs once per turn but the
        # MindLoop instance is long-lived across turns.
        self._turn_t0 = time.time()
        self._turn_first_speak_ms = None
        self._turn_think_chars = 0
        self._turn_speak_chars = 0
        self._turn_passes = 0

        def on_stream_end(index: int, reasoning: str, end: End | None) -> None:
            nonlocal first_end
            # 延遲壓縮 Part 1 Task 2: accumulate this pass's think-chars +
            # advance the turn-scoped pass counter, on every pass including a
            # cancelled-mid-stream one (its partial thinking is still real
            # streamed content, not synthesized).
            self._turn_think_chars += len(reasoning)
            self._turn_passes = index + 1
            if index == 0:
                first_end = end

        def before_tools() -> None:
            self._turn_had_tool = True

        def on_pass(p: PassRecord) -> None:
            results = [o.result for o in p.outcomes]
            tool_calls = [
                {"id": o.call.id, "name": o.call.name, "arguments": o.call.arguments}
                for o in p.outcomes
            ]
            if self._cascade_logger is not None:
                try:
                    self._cascade_logger.log_iter(
                        turn_id=turn_id,
                        iter=p.index,
                        reasoning=p.reasoning,
                        speech=p.speech,
                        tool_calls=tool_calls,
                        results=results,
                        duration_ms=int((time.monotonic() - p.started) * 1000),
                    )
                except Exception:
                    logger.exception("cascade_logger.log_iter failed; continuing")

            # P1f trace: same-site, same-tuple capture as cascade_logger
            # above — both writers serialize from the identical locals so
            # the two logs can never drift apart. Trace stores raw/full
            # content (T-C2): the whole reasoning (cascade_log caps it), and
            # result `detail` NOT truncated to [:500] like cascade_log's copy
            # is. `input_messages_delta` is the byte-verbatim authority for
            # what the conversation appended since its last traced pass (this
            # turn's message, an interrupted turn's leftovers, the previous
            # pass's assistant + tool messages): nothing is appended while a
            # pass runs, so `messages[conv.traced:p.sent]` is what was sent.
            # `dict(m)` shallow-copies each message so later appends can't
            # alter the captured delta.
            if turn_trace is not None:
                try:
                    turn_trace.add_pass(
                        pass_idx=p.index,
                        input_messages_delta=[
                            dict(m) for m in conv.messages[conv.traced:p.sent]
                        ],
                        reasoning=p.reasoning,
                        speech=p.speech,
                        tool_calls=tool_calls,
                        results=[
                            {
                                "tool_call_id": o.call.id,
                                "tool_name": o.result.tool_name,
                                "status": o.result.status,
                                "success": o.result.success,
                                "detail": o.result.detail or "",
                            }
                            for o in p.outcomes
                        ],
                        active_tools=sorted(self._runnable_tools(offered)),
                        is_reflection=self._moment == "reflection",
                        safe_mode=self._state.safe_mode,
                        external=self._ctx.external_ctx,
                        latency_ms=p.stream_ms,
                        msg_range=(conv.traced, p.sent),
                    )
                    conv.traced = p.sent
                except Exception:
                    logger.exception(
                        "trace add_pass failed; continuing (this pass omitted from trace)"
                    )

            # Recall output is in-context external content from here on
            # (spec §3.2 — Recall never appears in the drained batch), so a
            # PinSelf on a later pass sees external_ctx=True.
            for r in results:
                if r.tool_name == "Recall" and r.success:
                    self._ctx.external_ctx = True

        try:
            self._cascade_ctx = CascadeCtx()
            turn_id = self._cascade_logger.start_turn() if self._cascade_logger is not None else None
            self._turn_id = turn_id  # Task 3: mirror for `_run_one_turn`'s drain gate
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
            result = await TurnRunner(self._llm).run(Turn(
                kind=self._binding_kind(),
                principal=self._ctx.principal,
                thread=conv,
                tools=tools,
                execute=lambda name, args: self._dispatch_bound(name, args, offered),
                ctx=self._ctx,
                max_tokens=MIND_MAX_TOKENS,
                think_budget=self._think_budget_tokens,
                purpose="cascade",
                speak=lambda sentence: self._speak_sentence(sink, sentence),
                cancel=self._cascade_ctx,
                on_usage=self._on_turn_usage,
                on_stream_end=on_stream_end,
                before_tools=before_tools,
                on_pass=on_pass,
            ))
            if result.outcome == "brain_down":
                if result.error.kind == "server":
                    # A bad or too-long message would stay and fail every
                    # later call: the next turn opens a new page.
                    conv.server_error = True
                raise result.error
            if result.ended_by == "safe_mode":
                self._enter_safe_mode(result.reason)
        finally:
            if turn_trace is not None:
                # speech = this turn's full spoken text; silence = none spoken.
                speech = self._collect_turn_speech()
                turn_trace.finish(speech=speech, silence=(speech == ""))
            # 延遲壓縮 Part 1 Task 2: turn-level latency record. `_turn_t0`
            # guards against a `finally` reached without the turn ever
            # starting (defensive; `_llm_iterate` always sets it first thing).
            # `record()` itself never raises (log-and-continue) — telemetry
            # must not be able to fail a turn.
            if self._turn_latency_recorder is not None and self._turn_t0 is not None:
                total_ms = (time.time() - self._turn_t0) * 1000.0
                rec = TurnLatencyRecord(
                    ts=time.time(),
                    first_speak_ms=self._turn_first_speak_ms,
                    think_chars=self._turn_think_chars,
                    speak_chars=self._turn_speak_chars,
                    total_ms=total_ms,
                    ttft_ms=first_end.ttft_ms if first_end is not None else None,
                    mode="deliberate",  # Part 2 改為 self._think_mode
                    n_passes=self._turn_passes,
                    had_tool_call=self._turn_had_tool,
                    perception_kinds=self._turn_perception_kinds,
                    turn_id=turn_id,
                    cache_n=first_end.usage.cache_n if first_end is not None else None,
                    prompt_n=first_end.usage.prompt_n if first_end is not None else None,
                    finish_reason=first_end.finish_reason if first_end is not None else None,
                    conversation=conv.key,
                    epoch=conv.epoch if conv.key is not None else None,
                )
                await self._turn_latency_recorder.record(rec)
            self._cascade_ctx = None

    def _collect_turn_speech(self) -> str:
        """Full spoken text for the turn currently in flight (P1f trace).

        Joins ``self._turn_speech`` — a turn-local buffer of FULL sentence
        text, cleared once per turn in `_run_one_turn` before `_llm_iterate`
        runs and appended to by `_speak_sentence` as
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

    def _emit_sentence(self, sink, sentence: str) -> None:
        """Push one streamed sentence to `sink`, addressed to the current
        origin's channel when that origin is registered external (spec
        §3.1) — else plain TextChunk, unchanged (internal/unregistered
        origins, including the None origin of internal turns).

        Guards the single outbound chokepoint: a whitespace-only "sentence"
        (e.g. the newlines that open a reply right after the thinking, split
        out by `SentenceChunker` — regression, see `test_mind_loop_empty_speech_chunk.py`)
        has no deliverable content and must never reach the sink. Some
        transports (e.g. Discord) reject an empty/whitespace message
        outright, and a raised exception there must not be able to take down
        the whole turn/connection over content that was never meaningful.

        Self-directed agenda (spec 2026-07-07 §5.3, R1-M1): a pure
        AgendaMoment turn is her thinking internally, not talking — if she
        streams text, it must not reach any local voice/UI sink (which is
        what an origin-less internal turn's sink resolves to). The turn's
        recent_outputs/_turn_speech bookkeeping in the callers still records
        it for trace/audit; only the outbound sink write is suppressed here,
        at this single chokepoint, so reflection/reactive/every other turn
        kind is unaffected (``TurnPolicy.speaks``, turn_policy.POLICIES).

        Dedicated diary turn (spec 2026-07-07 §2.3, Task 7): same
        chokepoint, same rationale — a pure DiaryMoment turn is private
        journaling, not a conversation, so any naked text she streams (e.g.
        commentary alongside the WriteDiary call) must not leak to a
        connected sink either.

        延遲壓縮 Part 1 Task 2: also the single chokepoint for first-speak
        latency + speak_chars telemetry — measured AFTER both guards above,
        so a suppressed/whitespace-only sentence never counts as "spoke".
        """
        if not sentence.strip():
            return
        if not POLICIES[self._moment].speaks:
            return
        if self._turn_first_speak_ms is None and self._turn_t0 is not None:
            self._turn_first_speak_ms = (time.time() - self._turn_t0) * 1000.0
        self._turn_speak_chars += len(sentence)
        origin = self._ctx.current_origin
        registry = self._ctx.channel_registry
        if origin and registry is not None and registry.locus_of(origin) == "external":
            sink.put_nowait(AddressedText(channel_id=origin, text=sentence))
        else:
            sink.put_nowait(TextChunk(text=sentence))

    def _speak_sentence(self, sink, sentence: str) -> None:
        """One chunked sentence: out through `_emit_sentence`, plus the
        turn's speech bookkeeping (recent_outputs + full-text `_turn_speech`)."""
        self._emit_sentence(sink, sentence)
        self._state.recent_outputs.append(OutputRecord(
            kind="Speech",
            t=time.time(),
            summary=f"spoke: {sentence[:60]}",
            origin_tier=self._ctx.origin_tier,
        ))
        self._turn_speech.append(sentence)

    def _runnable_tools(
        self, offered: dict[str, type[BaseModel]]
    ) -> dict[str, type[BaseModel]]:
        """What this turn may run: ``_active_tool_registry()`` ∩ ``offered``.

        Never more than the tier offers (harness §6.2 as amended by plan
        2026-10-18)."""
        return {
            n: c for n, c in self._active_tool_registry().items() if n in offered
        }

    async def _dispatch_bound(
        self, name: str, arguments: dict, offered: dict[str, type[BaseModel]]
    ) -> ToolResult | None:
        """Execution-time binding (plan 2026-10-18 decision 3), through the
        ``ToolExecutor`` (which writes the call's log record). The model is
        shown every ``offered`` tool of this tier; only this turn's
        ``_runnable_tools(offered)`` may run. An offered-but-unbound call is
        refused with the tools available now — NOT a failure: no tool memory,
        no action log, no safe-mode count (the Turn Runner caps repeats via
        UNBOUND_REJECT_LIMIT). Every other call, a name outside ``offered``
        included, goes to ``_dispatch_tool`` against the runnable set, where
        an unknown tool is a failure as before. Once the turn is tainted
        (``ctx.tainted``, spec §6.5 point 2) the executor narrows the
        runnable set further; the flag lives on ``ctx`` so it outlives this
        per-call executor."""
        executor = ToolExecutor(
            self._ctx, offered=offered, bound=self._runnable_tools(offered),
            principal=self._ctx.principal, kind=self._binding_kind(),
            dispatch=self._dispatch_tool,
        )
        return await executor.run(name, arguments)

    async def _dispatch_tool(
        self, name: str, arguments: dict,
        registry: dict[str, type[BaseModel]] | None = None,
    ) -> ToolResult | None:
        """Dispatch via shared dispatch_one (spec §3.6) against ``registry``
        (default ``_active_tool_registry()``), then record the outcome
        into Doll's tool memory (Spec B Layer 1 — live-only). Also — for
        owner/internal turns only (C1) — append a whitelisted action line to
        the day's action log."""
        prior_mood = self._ctx.mind_state.mood.emotion   # snapshot before dispatch (Mood-change)
        if registry is None:
            registry = self._active_tool_registry()
        r = await dispatch_one(name, arguments, self._ctx, registry)
        record_tool_outcome(self._ctx.mind_state, name, r)
        if name == "WriteDiary":
            self._turn_wrote_diary = True
        # 🔒 C1: never log a stranger turn's action into the owner-tier transcript.
        # A failed call (bad arguments, e.g. a MoodTool emotion that is not one
        # English word, or a raise) did nothing, so it is not logged either.
        failed = r is not None and not r.success
        if self._ctx.origin_tier != "external_public" and not failed:
            phrase = action_phrase_for_tool(name, arguments, prior_mood)
            if phrase:
                try:
                    await append_action_log(
                        transcripts_root=self._ctx.transcripts_root,
                        memsearch=self._ctx.memsearch,
                        phrase=phrase,
                    )
                except Exception:
                    logger.exception("action-log write (tool) failed; continuing")
        return r

    def shutdown(self) -> None:
        """Signal the loop to stop. Unblocks any pending drain()."""
        self._shutdown = True
        self._queue.shutdown()

