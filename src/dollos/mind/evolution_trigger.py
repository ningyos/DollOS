"""慢變演化 Mode-B trigger + skeptic driver (spec §3.3).

Plan 2 implements ONLY Mode B: the verdict-only re-verdict pass that runs the
skeptic on a ``pending.status == "awaiting_skeptic"`` slot (counter or external
origin), gated ONLY on conversation-idle (condition 1) + no-consolidation-
running (condition 4) + the §3.3 failure-table 1h skeptic-error cooldown
(``ERROR_COOLDOWN_S``, anchored on ``pending.last_error_ts``). Mode A (keeper),
the material gate, HWM/interval
dynamics, and last_evolution_attempt bookkeeping are Plan 3. The skeptic is a
driver-fed ephemeral agent (KEEPER_TOOLS Report-only, same shape as
run_consolidation's keeper). For counter/external origins its scope is (a)+(b)
ONLY (spec §3.3 sovereignty finding).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo
from dollos.mind import self_history
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
    """Background observer: Mode-B verdict-only re-verdict pass (spec §3.3)."""

    POLL_INTERVAL_S = 5.0
    # Spec §3.3 failure table: 1h error-cooldown after a skeptic error. Without
    # it the 5s poll would retry immediately and 3 transient errors could
    # expire a valid slot in ~15s (review I3).
    ERROR_COOLDOWN_S = 3600.0

    def __init__(self, *, state, adapter, renderer, memsearch, memory_root: Path,
                 transcripts_root: Path, tool_output_store, pack_identity,
                 consolidation_trigger, idle_threshold_s: int = 600,
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
        self._idle_threshold_s = idle_threshold_s
        self._max_tokens = max_tokens
        self._agent_timeout_s = agent_timeout_s
        self._shutdown = False
        self.current_task: asyncio.Task | None = None

    @property
    def _slot_path(self) -> Path:
        return self._memory_root / "self_evolution" / "pending.json"

    @property
    def _history_path(self) -> Path:
        return self._memory_root / "self_history.jsonl"

    @property
    def _current_self_path(self) -> Path:
        return self._memory_root / "current_self.md"

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
        if slot.last_error_ts is not None and \
                now - slot.last_error_ts < self.ERROR_COOLDOWN_S:
            return False  # spec §3.3 failure table: 1h error-cooldown
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
            slot.last_error_ts = time.time()  # 1h cooldown anchor (spec §3.3)
            if slot.verdict_errors >= evo.VERDICT_ERRORS_BOUND:
                # Deterministic bound (spec §3.3): a failing skeptic must not
                # wedge condition 5 forever.
                logger.warning("evolution: verdict_errors bound hit → expire")
                evo.log_or_raise(self._history_path, kind=evo.EVO_EXPIRE,
                                 text=slot.candidate, kind_origin=slot.kind,
                                 hwm_before=slot.hwm_before)
                evo.clear_slot(self._slot_path)
                evo._restore_file(self._current_self_path, old_sanctioned)
            else:
                evo.log_or_raise(self._history_path, kind=evo.EVO_ERROR,
                                 detail="skeptic error", kind_origin=slot.kind)
                evo.save_slot(self._slot_path, slot)
            return

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
            evo._restore_file(self._current_self_path, old_sanctioned)

    async def run(self) -> None:
        """Poll loop. Cancelled by kernel at shutdown or via cancel_current() on UserSpoke."""
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                if not self._should_reverdict(time.time()):
                    continue
                # No outer wait_for: the skeptic timeout is handled INSIDE
                # _reverdict_once so it flows through the verdict_errors bound
                # + cooldown (an outer timeout would cancel the error handling
                # itself and retry forever — review Important).
                self.current_task = asyncio.create_task(self._reverdict_once())
                try:
                    await self.current_task
                finally:
                    self.current_task = None
            except asyncio.CancelledError:
                if self._shutdown:
                    raise
                # UserSpoke cancel — the slot stays awaiting_skeptic; next idle re-runs.
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
