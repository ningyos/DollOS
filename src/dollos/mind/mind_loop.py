"""MindLoop — the single persistent coroutine that IS Doll's consciousness."""
from __future__ import annotations

import logging
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dollos.cascade.cascade_ctx import CascadeCtx
from dollos.cascade.sentence_chunker import SentenceChunker
from dollos.cascade.tool_loop import ToolResult, dispatch_one
from dollos.mind.tool_memory import record_tool_outcome, render_tool_outcomes, tool_habits_search
from dollos.ipc.messages import TextChunk
from dollos.llm.templates import build_voice_first_grammar
from dollos.memory_writer import append_transcript
from dollos.mind.associative_search import associative_search
from dollos.tools import NoteToolLesson
from dollos.mind.mind_ctx import MindCtx
from dollos.mind.mind_prompt import render_mind
from dollos.mind.mind_state import (
    MindState,
    OutputRecord,
    Perception,
    save_state,
)
from dollos.mind.perception_queue import PerceptionQueue
from dollos.stream_events import SpeakChunk, ToolCallReady
from dollos.tool_parser import ToolStreamParser
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
# decision; `NoteMemory` returns a "memory noted: …" confirmation she does NOT
# need to observe — re-feeding it would cost an extra decode pass on the
# project's #1-concern latency path. Tool FAILURES of ANY tool still re-feed
# (external grounding so Doll is told and can fix her mistake) — this allowlist
# gates SUCCESS only.
IN_TURN_REFEED_TOOLS = frozenset({"Recall"})

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
    ) -> None:
        self._state = state
        self._queue = queue
        self._ctx = ctx
        self._llm = llm
        self._system_prompt = system_prompt
        self._primary_language = primary_language
        self._persist_path = state_persist_path
        self._tool_registry = tool_registry or {}
        self._system_pulse = system_pulse
        self._cognition = cognition
        self._wal = wal
        self._cascade_logger = cascade_logger
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

        # No-fallback (spec §3.3): a grammar build failure is a tool-set config
        # error. Let it raise at startup — the daemon must refuse to run with a
        # half-built / unconstrained tool set rather than silently degrade.
        if self._tool_registry:
            self._grammar = build_voice_first_grammar(
                list(self._tool_registry.values())
            )
        else:
            self._grammar = None

    async def run(self) -> None:
        """Main loop. Runs until shutdown."""
        while not self._shutdown:
            try:
                await self.iterate()
            except Exception:
                logger.exception("MindLoop iteration crashed; continuing")

    async def iterate(self) -> None:
        """One iteration: drain → sync → render → llm → execute → persist."""
        perceptions = await self._queue.drain()
        if not perceptions:
            # drain() returned empty — shutdown signaled; skip this iteration
            return
        for p in perceptions:
            self._state.recent_perceptions.append(p)
            if p.kind == "UserSpoke":
                self._state.last_user_at = p.t
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
        try:
            # Pre-render [Tool outcomes] block for reflection turns (Spec B Task 6).
            tool_outcomes_block = None
            if self._is_reflection:
                tool_outcomes_block = render_tool_outcomes(
                    self._state.tool_stats, self._state.recent_tool_failures
                )

            # Render prompt
            prompt = render_mind(
                self._state,
                memsearch_hits,
                self._system_prompt,
                pulse_block=pulse_block,
                cognition_block=cognition_block,
                associative_hits=associative_hits,
                primary_language=self._primary_language,
                tool_outcomes_block=tool_outcomes_block,
                tool_habits_hits=tool_habits_hits,
            )

            # Call LLM (streams text → sink; dispatches tool calls inline)
            await self._llm_iterate(prompt)
        finally:
            # Signal end-of-turn to the connection pump: a None turn-separator
            # is converted to TurnEnd by the IPC pump. Always fires once per
            # real turn (even on error) so a text/IPC client never hangs
            # waiting for turn_end. Voice path: TTSObservingSink passes None
            # through unchanged (not a TextChunk, so no TTS side effect).
            self._ctx.sink_resolver().put_nowait(None)

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

        # Update counters + persist
        self._state.iter_count += 1
        self._state.last_iter_at = time.time()
        saved = save_state(self._state, self._persist_path)

        # Truncate WAL through the highest seq among consumed perceptions —
        # ONLY when the save durably succeeded. After a successful save_state the
        # state durably reflects these perceptions, so they no longer need to be
        # replayed on next startup. If the save FAILED we must NOT truncate, so
        # the perceptions remain in the WAL and are replayed (and re-attempted)
        # on next boot.
        if self._wal is not None and perceptions and saved:
            last_seq = max(
                (p.seq for p in perceptions if p.seq is not None),
                default=None,
            )
            if last_seq is not None:
                self._wal.truncate_through(last_seq)
        elif self._wal is not None and perceptions and not saved:
            logger.warning(
                "save failed; skipping WAL truncation to preserve "
                "perceptions for replay"
            )

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
            query = " ".join(str(p.data) for p in last3)[:500]
        if not query:
            return []
        try:
            return await self._ctx.memsearch.search(query, top_k=10)
        except Exception:
            logger.exception("memsearch query failed; continuing with empty hits")
            return []

    def _active_tool_registry(self) -> dict[str, type[BaseModel]]:
        """The tool registry in force for this pass.

        safe_mode → read-only subset (SAFE_MODE_TOOLS); safe_mode has priority.
        reflection turn → full registry + NoteToolLesson.
        otherwise → base registry.
        """
        if self._state.safe_mode:
            return {
                n: c for n, c in self._tool_registry.items()
                if n in SAFE_MODE_TOOLS
            }
        if self._is_reflection:
            return {**self._tool_registry, "NoteToolLesson": NoteToolLesson}
        return self._tool_registry

    def _active_grammar(self) -> str | None:
        """Grammar for this pass, built from the active tool registry.

        safe_mode → lazily-built+cached reduced grammar (no-fallback: raises on
        build failure, never degrades to grammar=None; spec §8.3).
        reflection turn → lazily-built+cached expanded grammar (MAIN_TOOLS +
        NoteToolLesson).
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

    async def _llm_iterate(self, prompt: str) -> None:
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
        sink = self._ctx.sink_resolver()
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

        try:
            self._cascade_ctx = CascadeCtx()
            turn_id = self._cascade_logger.start_turn() if self._cascade_logger is not None else None
            for pass_idx in range(MAX_SYNC_REFEED_PASSES):
                if self._cascade_ctx.cancelled:
                    logger.info("cascade cancelled at pass boundary; exiting cleanly")
                    return

                pass_start = time.monotonic()
                raw_buf, results, tool_calls = await self._stream_one_pass(
                    prompt=prompt,
                    messages=messages,
                    first_pass=(pass_idx == 0),
                    sink=sink,
                )

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

    def _flush_chunker(self, chunker: SentenceChunker, sink) -> None:
        for sentence in chunker.flush():
            if sentence:
                sink.put_nowait(TextChunk(text=sentence))
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
                sink.put_nowait(TextChunk(text=sentence))
                self._state.recent_outputs.append(OutputRecord(
                    kind="Speech",
                    t=time.time(),
                    summary=f"spoke: {sentence[:60]}",
                ))
                self._turn_speech.append(sentence)
            return None
        elif isinstance(event, ToolCallReady):
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

