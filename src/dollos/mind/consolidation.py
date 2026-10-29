"""B2 sleep-time consolidation — driver + trigger.

Driver reads a target day's transcript, feeds it inline to a memory-keeper
agent (KEEPER_TOOLS only), and writes the returned bullets to
consolidated/{date}.md. Candidate facts are pull-only (see mind_loop gating).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date as _date
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.memory_writer import is_action_log_line
from dollos.mind.mind_state import save_state
from dollos.mind.profile_keeper import run_profile_keeper
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

_KEEPER_TASK = """Read the transcript below and extract concise, neutral, deduplicated candidate facts: your owner's stable preferences and habits, how the two of you are getting along, patterns worth remembering long-term. State them as observations ("My owner prefers X"), not self-declarations ("I am X"). Merge duplicates, drop what's outdated. If you're unsure, leave it out; better nothing than filler. Accuracy over quantity. Write each fact in English, even when the transcript is in another language. Return the result in the details field of the Report tool, one markdown bullet per line.

Transcript:
{transcript}
"""


async def run_consolidation(
    *,
    target_date: str,
    model,
    renderer,
    memsearch,
    memory_root: Path,
    transcripts_root: Path,
    tool_output_store,
    consolidated_dir: Path,
    max_tokens: int = 2048,
    transcript_tail_chars: int = 8000,
    think_budget_tokens: int = 1024,
) -> bool:
    """Consolidate one day's transcript into consolidated/{date}.md.

    ``max_tokens`` is the keeper's answer budget per call;
    ``think_budget_tokens`` caps its thinking on top.

    Returns True on success (file written + indexed), False otherwise.
    Raises CancelledError through (caller treats as cancel → no write).
    """
    src = transcripts_root / f"{target_date}.md"
    if not src.exists():
        logger.info("consolidation: no transcript for %s; skip", target_date)
        return False
    raw = src.read_text(encoding="utf-8")
    # Isolation (spec §3 I2): the keeper extracts 主人 preferences from a
    # conversation 逐字稿 — action-log lines are not conversation, drop them
    # so they neither mislead extraction nor starve the tail budget.
    convo = "\n".join(ln for ln in raw.splitlines() if not is_action_log_line(ln))
    transcript = convo[-transcript_tail_chars:]

    system = renderer.render("subagent_scaffolding")

    report = await run_agent(
        task=_KEEPER_TASK.format(transcript=transcript),
        system=system,
        model=model,
        renderer=renderer,
        memory_root=memory_root,
        memsearch=memsearch,
        transcripts_root=transcripts_root,
        tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, kind="keeper",
        max_tokens=max_tokens,
        think_budget=think_budget_tokens,
        purpose="consolidation",
        shell_runner=None,
    )
    if not report or not report.get("details"):
        logger.warning("consolidation: keeper returned no report for %s", target_date)
        return False

    consolidated_dir.mkdir(parents=True, exist_ok=True)
    out = consolidated_dir / f"{target_date}.md"
    out.write_text(report["details"].strip() + "\n", encoding="utf-8")
    await memsearch.index_file(out)
    logger.info("consolidation: wrote %s", out)
    return True


class ConsolidationTrigger:
    """Background observer: fires run_consolidation when conversation is idle.

    Trigger conditions (all must hold):
    1. conversation_idle = now - max(last_user_at, last_iter_at) >= idle_threshold_s
    2. user_turn_count > last_consolidation_turn (new conversation material)
    3. now - last_consolidation_at >= min_interval_s (cooldown)

    SystemPulse.idle_s is an optional gate: only vetoes when a fresh idle_s is
    available AND it is below the threshold. None (Wayland/headless/disabled)
    is ignored — does not veto.

    State update semantics (spec §3.2):
    - last_consolidation_at: updated on every attempt (success OR failure), to
      prevent 5-second retry storms on persistent failures.
    - last_consolidation_turn / last_consolidated_date: updated only on success.

    Profile keeper (remember-corrections spec §2.2, plan decision 5): after a
    SUCCESSFUL consolidation for a date — and with consolidation's state
    already saved — the keeper runs for the same date when
    ``profile_keeper_enabled``. A failed consolidation (no transcript
    included) skips it with one log line. Its errors are logged and change
    neither consolidation's result nor its state; no retry, no schedule of
    its own. A returning owner cancels it like consolidation.

    Trigger saves state itself (it runs outside mind_loop.iterate).
    """

    POLL_INTERVAL_S = 5.0

    def __init__(
        self,
        *,
        state,
        persist_path,
        model,
        renderer,
        memsearch,
        memory_root: Path,
        transcripts_root: Path,
        tool_output_store,
        consolidated_dir: Path,
        system_pulse=None,
        idle_threshold_s: int = 300,
        min_interval_s: int = 3600,
        max_tokens: int = 2048,
        agent_timeout_s: int = 120,
        transcript_tail_chars: int = 8000,
        energy_enabled: bool = False,
        restore_per_tick: float = 0.05,
        energy_idle_threshold_s: int = 600,
        energy_restore_debounce_s: int = 300,
        think_budget_tokens: int = 1024,
        profile_keeper_enabled: bool = False,
        profile_keeper_max_tokens: int = 2048,
        profile_keeper_think_budget_tokens: int = 1024,
        profile_keeper_transcript_tail_chars: int = 8000,
        self_profile_max_chars: int = 4000,
    ) -> None:
        self._state = state
        self._persist_path = persist_path
        self._model = model
        self._renderer = renderer
        self._memsearch = memsearch
        self._memory_root = memory_root
        self._transcripts_root = transcripts_root
        self._tool_output_store = tool_output_store
        self._consolidated_dir = consolidated_dir
        self._system_pulse = system_pulse
        self._idle_threshold_s = idle_threshold_s
        self._min_interval_s = min_interval_s
        self._max_tokens = max_tokens
        # [llm].think_budget_tokens: caps the keeper's thinking per call.
        self._think_budget_tokens = think_budget_tokens
        self._agent_timeout_s = agent_timeout_s
        self._transcript_tail_chars = transcript_tail_chars
        self._shutdown = False
        self.current_task: asyncio.Task | None = None
        # B3 energy restore params
        self._energy_enabled = energy_enabled
        self._restore_per_tick = restore_per_tick
        self._energy_idle_threshold_s = energy_idle_threshold_s
        self._energy_restore_debounce_s = energy_restore_debounce_s
        # [profile_keeper] (remember-corrections plan Task 4); the keeper
        # shares consolidation's agent_timeout_s. self_profile_max_chars is
        # the profile's shared cap ([self_profile].max_chars).
        self._profile_keeper_enabled = profile_keeper_enabled
        self._profile_keeper_max_tokens = profile_keeper_max_tokens
        self._profile_keeper_think_budget_tokens = profile_keeper_think_budget_tokens
        self._profile_keeper_transcript_tail_chars = profile_keeper_transcript_tail_chars
        self._self_profile_max_chars = self_profile_max_chars

    def _maybe_restore_energy(self, now: float) -> None:
        """Restore energy if user has been idle long enough (B3 spec §3.3).

        Decoupled from consolidation target — runs regardless of whether there
        is a sealed transcript to process. Uses last_user_at (not last_iter_at)
        as the idle baseline so tool-result/schedule ticks don't suppress recovery.
        """
        if not self._energy_enabled:
            return
        if now - self._state.last_user_at < self._energy_idle_threshold_s:
            return
        if now - self._state.last_energy_restore_at < self._energy_restore_debounce_s:
            return
        self._state.energy = min(1.0, self._state.energy + self._restore_per_tick)
        self._state.last_energy_restore_at = now
        save_state(self._state, self._persist_path)

    def _conversation_idle(self, now: float) -> float:
        return now - max(self._state.last_user_at, self._state.last_iter_at)

    def _should_consolidate(self, now: float) -> bool:
        """Return True iff all three trigger conditions are satisfied."""
        if self._conversation_idle(now) < self._idle_threshold_s:
            return False
        if self._state.user_turn_count <= self._state.last_consolidation_turn:
            return False
        if now - self._state.last_consolidation_at < self._min_interval_s:
            return False
        # optional SystemPulse gate: only vetoes when a fresh idle_s is available
        if self._system_pulse is not None:
            idle = self._system_pulse.latest_idle_s()
            if idle is not None and idle < self._idle_threshold_s:
                return False
        return True

    def _pick_target_date(self, today: str) -> str | None:
        """Oldest sealed (date < today) transcript date after last_consolidated_date."""
        watermark = self._state.last_consolidated_date
        candidates: list[str] = []
        if self._transcripts_root.exists():
            for f in self._transcripts_root.glob("*.md"):
                d = f.stem  # YYYY-MM-DD
                try:
                    _date.fromisoformat(d)
                except ValueError:
                    continue
                if d < today and d > watermark and f.stat().st_size > 0:
                    candidates.append(d)
        return min(candidates) if candidates else None

    async def run(self) -> None:
        """Poll loop. Cancelled by kernel at shutdown or via cancel_current() on UserSpoke."""
        while not self._shutdown:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                now = time.time()
                # B3: energy restore is independent of consolidation target.
                self._maybe_restore_energy(now)
                if not self._should_consolidate(now):
                    continue
                today = _date.today().isoformat()
                target = self._pick_target_date(today)
                # Attempt timestamp advances regardless of success/failure (cooldown).
                self._state.last_consolidation_at = now
                if target is None:
                    # Idle + new turns but nothing sealed to do yet; persist cooldown.
                    save_state(self._state, self._persist_path)
                    continue
                await self._cycle(target)
            except asyncio.CancelledError:
                # Cancelled by a returning user (or shutdown); persist cooldown.
                save_state(self._state, self._persist_path)
                if self._shutdown:
                    raise
                # Not shutdown → user spoke, consolidation interrupted; continue loop.
            except Exception:
                logger.exception("consolidation trigger iteration failed; continuing")
                try:
                    save_state(self._state, self._persist_path)
                except Exception:
                    pass

    async def _cycle(self, target: str) -> bool:
        """Consolidate ``target``, save state, then run the profile keeper
        for it (class doc). Returns consolidation's result."""
        ok = await self._run_once(target)
        if ok:
            self._state.last_consolidation_turn = self._state.user_turn_count
            self._state.last_consolidated_date = target
        save_state(self._state, self._persist_path)
        if not self._profile_keeper_enabled:
            return ok
        if not ok:
            logger.info("profile keeper: consolidation of %s did not succeed; skip",
                        target)
            return ok
        await self._run_keeper(target)
        return ok

    async def _run_keeper(self, target: str) -> None:
        """Run the profile keeper for ``target`` as ``current_task`` (so
        cancel_current reaches it), under consolidation's timeout. Errors
        are logged; CancelledError propagates like consolidation's."""
        self.current_task = asyncio.create_task(
            asyncio.wait_for(
                run_profile_keeper(
                    target_date=target,
                    model=self._model,
                    renderer=self._renderer,
                    memsearch=self._memsearch,
                    memory_root=self._memory_root,
                    transcripts_root=self._transcripts_root,
                    tool_output_store=self._tool_output_store,
                    max_tokens=self._profile_keeper_max_tokens,
                    think_budget_tokens=self._profile_keeper_think_budget_tokens,
                    transcript_tail_chars=self._profile_keeper_transcript_tail_chars,
                    self_profile_max_chars=self._self_profile_max_chars,
                ),
                timeout=self._agent_timeout_s,
            )
        )
        try:
            await self.current_task
        except asyncio.TimeoutError:
            logger.warning("profile keeper timed out for %s", target)
        except Exception:
            logger.exception("profile keeper run failed for %s", target)
        finally:
            self.current_task = None

    async def _run_once(self, target: str) -> bool:
        """Run consolidation for target date, wrapped in wait_for + create_task."""
        self.current_task = asyncio.create_task(
            asyncio.wait_for(
                run_consolidation(
                    target_date=target,
                    model=self._model,
                    renderer=self._renderer,
                    memsearch=self._memsearch,
                    memory_root=self._memory_root,
                    transcripts_root=self._transcripts_root,
                    tool_output_store=self._tool_output_store,
                    consolidated_dir=self._consolidated_dir,
                    max_tokens=self._max_tokens,
                    transcript_tail_chars=self._transcript_tail_chars,
                    think_budget_tokens=self._think_budget_tokens,
                ),
                timeout=self._agent_timeout_s,
            )
        )
        try:
            return await self.current_task
        except asyncio.TimeoutError:
            # Expected path: keeper agent did not finish within agent_timeout_s.
            logger.warning("consolidation timed out for %s", target)
            return False
        except Exception:
            # CancelledError (BaseException) is NOT caught here and propagates to
            # run() → except asyncio.CancelledError.
            logger.exception("consolidation run failed for %s", target)
            return False
        finally:
            self.current_task = None

    def cancel_current(self) -> None:
        """Cancel any in-flight consolidation task (called on UserSpoke)."""
        t = self.current_task
        if t is not None and not t.done():
            t.cancel()

    def shutdown(self) -> None:
        """Signal shutdown and cancel current task."""
        self._shutdown = True
        self.cancel_current()
