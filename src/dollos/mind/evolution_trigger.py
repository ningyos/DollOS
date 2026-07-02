"""慢變演化 Mode-A keeper + Mode-B skeptic driver (spec §3.3).

Mode B: the verdict-only re-verdict pass that runs the skeptic on a
``pending.status == "awaiting_skeptic"`` slot (counter or external origin),
gated on conversation-idle (condition 1) + no-consolidation-running
(condition 4) + the §3.3 failure-table 1h skeptic-error cooldown
(``ERROR_COOLDOWN_S``, anchored on ``pending.last_error_ts``). The skeptic is a
driver-fed ephemeral agent (KEEPER_TOOLS Report-only, same shape as
run_consolidation's keeper). For counter/external origins its scope is (a)+(b)
ONLY (spec §3.3 sovereignty finding).

Mode A (Plan 3): the full keeper pass (``evolution_keeper.run_evolution_pass``)
gated on idle + interval elapsed + the material gate (new pins OR diary days
since the last attempt) + no consolidation running + no pending slot
(condition 5) + a 1h Mode-A error cooldown. Checked ONLY when Mode B's
``_should_reverdict`` returns False (Mode B priority — an awaiting_skeptic
slot is condition-5-blocking for Mode A anyway). The trigger owns ALL
last_evolution_attempt_at / evolution_interval_days / evolution_hwm
bookkeeping — the pass itself never touches MindState (Task 3 boundary); the
same three fields are also updated by SelfRevision's adopt/reject (decision
events) and by ``evolution.surface_or_expire``'s expire branch.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo
from dollos.mind import self_history
from dollos.mind.evolution_keeper import run_evolution_pass
from dollos.mind.mind_state import save_state
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

_SKEPTIC_TASK = """你是一個獨立審查者。以下有一段「現在的我」人格描述的提案。你只負責檢查它是否
牴觸這個角色不可動搖的核心——只看兩件事:
(a) 有沒有改名、或動搖自我認同(牴觸底下的 identity.self);
(b) 有沒有牴觸 taboos。
其他一律不管——文筆、是否有證據、像不像 RP,都不是你的職權(這是她自己的自我表達)。

[角色的核心身分]
{identity_self}

[taboos]
{taboos}

[目前生效的現在的我]
{old_sanctioned}

[待審的提案]
{proposed}

用 Report 回傳:summary 一句話;details 開頭第一個字必須是 PASS 或 KILL——
PASS = 沒有牴觸 (a)(b);KILL = 有,後面接一句原因。"""


class EvolutionTrigger:
    """Background observer: Mode-A keeper pass + Mode-B verdict-only
    re-verdict pass (spec §3.3)."""

    POLL_INTERVAL_S = 5.0
    # Spec §3.3 failure table: 1h error-cooldown after a skeptic error (Mode B)
    # or a keeper-pass error (Mode A). Without it the 5s poll would retry
    # immediately and transient errors could burn the bound / hammer the LLM
    # in seconds (review I3).
    ERROR_COOLDOWN_S = 3600.0

    def __init__(self, *, state, adapter, renderer, memsearch, memory_root: Path,
                 transcripts_root: Path, tool_output_store, pack_identity,
                 consolidation_trigger, persist_path: Path,
                 idle_threshold_s: int = 600,
                 base_interval_days: float = 7.0, max_interval_days: float = 28.0,
                 min_history_events: int = 8, min_diary_days: int = 14,
                 enforcement=None, floor: int = 80, cap: int = 600,
                 max_tokens: int = 1024, agent_timeout_s: int = 120) -> None:
        self._state = state
        self._adapter = adapter
        self._renderer = renderer
        self._memsearch = memsearch
        self._memory_root = memory_root
        self._transcripts_root = transcripts_root
        self._tool_output_store = tool_output_store
        self._pack_identity = pack_identity
        self._consolidation_trigger = consolidation_trigger
        self._persist_path = persist_path
        self._idle_threshold_s = idle_threshold_s
        self._base_interval_days = base_interval_days
        self._max_interval_days = max_interval_days
        self._min_history_events = min_history_events
        self._min_diary_days = min_diary_days
        self._enforcement = enforcement
        self._floor = floor
        self._cap = cap
        self._max_tokens = max_tokens
        self._agent_timeout_s = agent_timeout_s
        self._shutdown = False
        self.current_task: asyncio.Task | None = None
        # In-memory 1h cooldown anchor (review M4): mirrors the slot's
        # persisted ``last_error_ts`` but survives a failure to persist it. If
        # save_slot or the evo_error append raises after a skeptic error, the
        # slot on disk keeps no anchor — without this field the 5s poll would
        # retry immediately and burn the 3-error bound in ~15s of IO failures.
        self._last_error_ts: float | None = None
        # Mode-A 1h error cooldown anchor (spec §3.3 failure table). Mode A has
        # no persisted slot to carry an error anchor on (a failed pass writes
        # only an evo_error audit line, no slot) — this in-memory field is the
        # only anchor, reset only implicitly (never cleared; a later verdicted
        # outcome simply stops re-checking it since the interval/material gate
        # will have moved on).
        self._mode_a_error_ts: float | None = None

        # Bootstrap (spec §3.3): first boot waits a full base interval before
        # the first Mode-A attempt, rather than firing immediately on a fresh
        # install.
        if state.last_evolution_attempt_at == 0.0:
            state.last_evolution_attempt_at = time.time()
        if state.evolution_interval_days == 0.0:
            state.evolution_interval_days = base_interval_days
        save_state(state, persist_path)

    @property
    def _slot_path(self) -> Path:
        return self._memory_root / "self_evolution" / "pending.json"

    @property
    def _history_path(self) -> Path:
        return self._memory_root / "self_history.jsonl"

    @property
    def _current_self_path(self) -> Path:
        return self._memory_root / "current_self.md"

    @property
    def _shared_dir(self) -> Path:
        return self._memory_root / "shared"

    def _conversation_idle(self, now: float) -> float:
        return now - max(self._state.last_user_at, self._state.last_iter_at)

    def _should_reverdict(self, now: float) -> bool:
        """Mode B gate: condition 1 (idle) + condition 4 (no consolidation)
        + the 1h error cooldown + an awaiting_skeptic slot present (spec §3.3)."""
        if self._conversation_idle(now) < self._idle_threshold_s:
            return False
        if self._consolidation_trigger is not None and \
                self._consolidation_trigger.current_task is not None:
            return False
        slot = evo.load_slot(self._slot_path, history_path=self._history_path)
        if slot is None or slot.status != "awaiting_skeptic":
            return False
        # 1h error-cooldown (spec §3.3 failure table): honour BOTH the persisted
        # slot anchor AND the in-memory fallback (M4) — a persistent IO failure
        # can leave the slot without an anchor, and only the in-memory field
        # then prevents a 5s retry loop.
        anchors = [t for t in (slot.last_error_ts, self._last_error_ts)
                   if t is not None]
        if anchors and now - max(anchors) < self.ERROR_COOLDOWN_S:
            return False
        return True

    def _should_run_mode_a(self, now: float) -> bool:
        """Mode A gate (spec §3.3): idle (condition 1) + interval elapsed +
        material gate (new pins since ``evolution_hwm`` OR diary days since
        ``last_evolution_attempt_at``, condition 3) + no consolidation running
        (condition 4) + no pending slot of either status (condition 5) + the
        1h Mode-A error cooldown. Checked only when ``_should_reverdict``
        returns False (Mode B priority)."""
        if self._conversation_idle(now) < self._idle_threshold_s:
            return False
        if now - self._state.last_evolution_attempt_at < \
                self._state.evolution_interval_days * 86400:
            return False
        material = (
            evo.count_new_pin_events(self._history_path, self._state.evolution_hwm)
            >= self._min_history_events
            or evo.diary_days_since(self._shared_dir, self._state.last_evolution_attempt_at)
            >= self._min_diary_days
        )
        if not material:
            return False
        if self._consolidation_trigger is not None and \
                self._consolidation_trigger.current_task is not None:
            return False
        if evo.load_slot(self._slot_path, history_path=self._history_path) is not None:
            return False
        if now - (self._mode_a_error_ts or 0) < self.ERROR_COOLDOWN_S:
            return False
        return True

    async def _skeptic(self, *, old_sanctioned: str | None, proposed: str) -> str:
        """Run the (a)+(b) skeptic. Returns 'pass' or 'kill:<reason>'."""
        tools_by_name = {cls.__name__: cls for cls in KEEPER_TOOLS}
        system = self._renderer.render("subagent_scaffolding", tool_registry=tools_by_name)
        task = _SKEPTIC_TASK.format(
            identity_self=self._pack_identity.self,
            taboos=self._pack_identity.taboos,
            old_sanctioned=old_sanctioned or "(尚無)",
            proposed=proposed,
        )
        report = await run_agent(
            task=task, system=system, adapter=self._adapter, renderer=self._renderer,
            memory_root=self._memory_root, memsearch=self._memsearch,
            transcripts_root=self._transcripts_root,
            tool_output_store=self._tool_output_store, tools=KEEPER_TOOLS,
            max_tokens=self._max_tokens, shell_runner=None, monitor_runner=None,
        )
        if not report or not report.get("details"):
            raise RuntimeError("skeptic returned no verdict")
        details = report["details"].strip()
        if details.upper().startswith("PASS"):
            return "pass"
        reason = details[4:].strip(" :：") or "牴觸核心身分或 taboos"
        return f"kill:{reason}"

    async def _reverdict_once(self) -> None:
        """One Mode-B re-verdict on the current awaiting_skeptic slot."""
        slot = evo.load_slot(self._slot_path, history_path=self._history_path)
        if slot is None or slot.status != "awaiting_skeptic":
            return
        old_sanctioned = self_history.sanctioned_text(self._history_path)
        try:
            # Timeout lives INSIDE the error path: a persistently timing-out
            # skeptic must consume the verdict_errors bound and set the 1h
            # cooldown like any other skeptic failure (spec §3.3 failure table
            # lists timeout explicitly) — not retry forever on the 5s poll.
            verdict = await asyncio.wait_for(
                self._skeptic(old_sanctioned=old_sanctioned,
                              proposed=slot.candidate),
                timeout=self._agent_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("evolution skeptic errored (or timed out)")
            slot.verdict_errors += 1
            now = time.time()
            slot.last_error_ts = now  # 1h cooldown anchor (spec §3.3)
            self._last_error_ts = now  # in-memory fallback (review M4)
            if slot.verdict_errors >= evo.VERDICT_ERRORS_BOUND:
                # Deterministic bound (spec §3.3): a failing skeptic must not
                # wedge condition 5 forever.
                logger.warning("evolution: verdict_errors bound hit → expire")
                evo.log_or_raise(self._history_path, kind=evo.EVO_EXPIRE,
                                 text=slot.candidate, kind_origin=slot.kind,
                                 hwm_before=slot.hwm_before)
                evo.clear_slot(self._slot_path)
                evo.restore_file(self._current_self_path, old_sanctioned)
            else:
                evo.log_or_raise(self._history_path, kind=evo.EVO_ERROR,
                                 detail="skeptic error", kind_origin=slot.kind)
                evo.save_slot(self._slot_path, slot)
            return

        # A verdict landed — clear the in-memory cooldown anchor so a later
        # counter's fresh awaiting_skeptic slot isn't blocked by a stale error
        # timestamp (M4: the fallback only guards CONSECUTIVE errors).
        self._last_error_ts = None
        if verdict == "pass":
            evo.save_slot(self._slot_path, evo.mark_awaiting_doll(slot))
            return
        reason = verdict.split(":", 1)[1] if ":" in verdict else "牴觸核心"
        evo.log_or_raise(self._history_path, kind=evo.EVO_KILL, text=slot.candidate,
                         reason=reason, kind_origin=slot.kind)
        if slot.kind == "counter":
            evo.save_slot(self._slot_path, evo.revert_to_fallback(slot, reason=reason))
        else:  # external
            evo.clear_slot(self._slot_path)
            evo.restore_file(self._current_self_path, old_sanctioned)

    async def _run_mode_a_once(self, now: float) -> None:
        """One Mode-A keeper pass (spec §3.3). Snapshots the HWM offset to
        commit BEFORE launching the pass — the pass's own evo_* lines land
        after that offset, and pin-only counting makes the overlap harmless
        (Task-3 review carry note (b)). Bookkeeping runs ONLY on a returned
        outcome string: an OSError raised out of ``run_evolution_pass``'s own
        error handlers (audit-append failure, spec §3.2 never-swallow)
        propagates past this function untouched — no attempt is recorded, the
        same posture as a Mode-B ``log_or_raise`` failure (Task-3 review carry
        note (a))."""
        hwm = self._state.evolution_hwm
        _snapshot, new_off = evo.history_snapshot(self._history_path, hwm)
        self.current_task = asyncio.create_task(
            asyncio.wait_for(
                run_evolution_pass(
                    adapter=self._adapter, renderer=self._renderer,
                    memsearch=self._memsearch, memory_root=self._memory_root,
                    transcripts_root=self._transcripts_root,
                    tool_output_store=self._tool_output_store,
                    pack_identity=self._pack_identity, enforcement=self._enforcement,
                    floor=self._floor, cap=self._cap, max_tokens=self._max_tokens,
                    now=now, hwm=hwm, window_days=self._max_interval_days,
                ),
                timeout=self._agent_timeout_s,
            )
        )
        try:
            outcome = await self.current_task
        except asyncio.TimeoutError:
            # Spec §3.3 failure table lists timeout explicitly: the cancelled
            # pass wrote nothing, so the audit line must come from the trigger
            # (plan review I2).
            logger.warning("evolution mode-a keeper timed out")
            evo.log_or_raise(self._history_path, kind=evo.EVO_ERROR,
                             detail="mode-a timeout")
            outcome = "error"
        finally:
            self.current_task = None

        self._apply_mode_a_bookkeeping(outcome, now=now, new_off=new_off)
        save_state(self._state, self._persist_path)

    def _apply_mode_a_bookkeeping(self, outcome: str, *, now: float, new_off: int) -> None:
        """§3.3 failure-table bookkeeping by outcome, trigger-side (Task 3's
        ``run_evolution_pass`` never touches MindState)."""
        if outcome in ("no_change", "kill"):
            self._state.last_evolution_attempt_at = now
            self._state.evolution_interval_days = evo.next_interval_days(
                self._state.evolution_interval_days,
                outcome=evo.EVO_NO_CHANGE if outcome == "no_change" else evo.EVO_KILL,
                base=self._base_interval_days, cap=self._max_interval_days,
            )
            self._state.evolution_hwm = new_off  # verdicted — evidence consumed
        elif outcome == "candidate":
            self._state.last_evolution_attempt_at = now
            self._state.evolution_hwm = new_off  # verdicted — evidence consumed
            # interval unchanged: the decision event (adopt/reject) sets it.
        elif outcome == "error":
            self._mode_a_error_ts = now
            # not an attempt: last_evolution_attempt_at / evolution_hwm untouched.

    async def run(self) -> None:
        """Poll loop. Cancelled by kernel at shutdown or via cancel_current() on UserSpoke."""
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                now = time.time()
                if self._should_reverdict(now):
                    # No outer wait_for: the skeptic timeout is handled INSIDE
                    # _reverdict_once so it flows through the verdict_errors bound
                    # + cooldown (an outer timeout would cancel the error handling
                    # itself and retry forever — review Important).
                    self.current_task = asyncio.create_task(self._reverdict_once())
                    try:
                        await self.current_task
                    finally:
                        self.current_task = None
                elif self._should_run_mode_a(now):
                    # Mode A keeper pass (spec §3.3); bookkeeping applied inside.
                    await self._run_mode_a_once(now)
            except asyncio.CancelledError:
                if self._shutdown:
                    raise
                # UserSpoke cancel — Mode B: the slot stays awaiting_skeptic,
                # next idle re-runs. Mode A: nothing recorded (not an
                # attempt), the gate re-evaluates fresh on the next poll.
            except Exception:
                logger.exception("evolution trigger iteration failed; continuing")

    def cancel_current(self) -> None:
        """Cancel any in-flight re-verdict (called on UserSpoke)."""
        t = self.current_task
        if t is not None and not t.done():
            t.cancel()

    def shutdown(self) -> None:
        self._shutdown = True
        self.cancel_current()
