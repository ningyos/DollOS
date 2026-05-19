"""MindLoop — the single persistent coroutine that IS Doll's consciousness."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from dollos.cascade.cascade_ctx import CascadeCtx
from dollos.cascade.sentence_chunker import SentenceChunker
from dollos.ipc.messages import TextChunk
from dollos.llm.templates import build_voice_first_grammar
from dollos.mind.associative_search import associative_search
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

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._state = state
        self._queue = queue
        self._ctx = ctx
        self._llm = llm
        self._system_prompt = system_prompt
        self._persist_path = state_persist_path
        self._tool_registry = tool_registry or {}
        self._system_pulse = system_pulse
        self._cognition = cognition
        self._shutdown = False
        self._cascade_ctx: CascadeCtx | None = None

        # Build GBNF grammar from tool registry to constrain LLM output.
        # Voice-first grammar emits </think> itself; no prefill needed.
        if self._tool_registry:
            try:
                self._grammar = build_voice_first_grammar(
                    list(self._tool_registry.values())
                )
            except Exception:
                logger.exception("failed to build voice_first grammar; running unconstrained")
                self._grammar = None
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

        # Render prompt
        prompt = render_mind(
            self._state,
            memsearch_hits,
            self._system_prompt,
            pulse_block=pulse_block,
            cognition_block=cognition_block,
            associative_hits=associative_hits,
        )

        # Call LLM (streams text → sink; dispatches tool calls inline)
        await self._llm_iterate(prompt)

        # Update counters + persist
        self._state.iter_count += 1
        self._state.last_iter_at = time.time()
        save_state(self._state, self._persist_path)

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

    @property
    def is_cascade_active(self) -> bool:
        """True iff _llm_iterate is currently running (a cascade is in flight)."""
        return self._cascade_ctx is not None

    def cancel_current_cascade(self) -> None:
        """Set cancel on the active cascade context, if any. No-op otherwise."""
        if self._cascade_ctx is not None:
            self._cascade_ctx.cancel()

    async def _llm_iterate(self, prompt: str) -> None:
        """Stream LLM output through voice_first parser.

        SpeakChunks → resolved sink + recent_outputs("Speech");
        ToolCallReady → dispatch the named tool inline (sequential).

        Honours cancel from `self._cascade_ctx` at every chunk / event /
        flush boundary so an external `cancel_current_cascade()` returns
        cleanly within ~one chunk window.
        """
        sink = self._ctx.sink_resolver()
        parser = ToolStreamParser(voice_mode=True)
        chunker = SentenceChunker()
        self._cascade_ctx = CascadeCtx()
        try:
            async for chunk in self._llm.stream_completion(
                system="",
                user=prompt,
                prefill="",  # voice_first grammar emits </think> itself
                max_tokens=2048,
                grammar=self._grammar,
                purpose="cascade",
            ):
                if self._cascade_ctx.cancelled:
                    logger.info("cascade cancelled mid-stream; exiting cleanly")
                    return
                if chunk.text:
                    for event in parser.feed(chunk.text):
                        if self._cascade_ctx.cancelled:
                            logger.info("cascade cancelled before event dispatch; exiting")
                            return
                        await self._handle_stream_event(event, sink, chunker)
                if chunk.done:
                    break

            if self._cascade_ctx.cancelled:
                return

            for event in parser.flush():
                if self._cascade_ctx.cancelled:
                    return
                await self._handle_stream_event(event, sink, chunker)

            if not self._cascade_ctx.cancelled:
                # Flush the chunker tail — emit any remaining buffered text.
                self._flush_chunker(chunker, sink)
        finally:
            self._cascade_ctx = None

    def _flush_chunker(self, chunker: SentenceChunker, sink) -> None:
        for sentence in chunker.flush():
            if sentence:
                sink.put_nowait(TextChunk(text=sentence))
                self._state.recent_outputs.append(OutputRecord(
                    kind="Speech",
                    t=time.time(),
                    summary=f"spoke: {sentence[:60]}",
                ))

    async def _handle_stream_event(self, event, sink, chunker) -> None:
        if isinstance(event, SpeakChunk):
            if not event.text:
                return
            for sentence in chunker.feed(event.text):
                sink.put_nowait(TextChunk(text=sentence))
                self._state.recent_outputs.append(OutputRecord(
                    kind="Speech",
                    t=time.time(),
                    summary=f"spoke: {sentence[:60]}",
                ))
        elif isinstance(event, ToolCallReady):
            await self._dispatch_tool(event.name, event.arguments)

    async def _dispatch_tool(self, name: str, arguments: dict) -> None:
        tool_cls = self._tool_registry.get(name)
        if tool_cls is None:
            logger.warning("unknown tool: %s", name)
            return
        try:
            tool = tool_cls(**arguments)
        except ValidationError as e:
            logger.warning("tool validation failed for %s: %s", name, e)
            return
        try:
            await tool.run(self._ctx)
        except Exception:
            logger.exception("tool %s failed", name)

    def shutdown(self) -> None:
        """Signal the loop to stop. Unblocks any pending drain()."""
        self._shutdown = True
        self._queue.shutdown()

