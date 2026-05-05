"""EventDispatcher — fan-out raw events to concurrent tasks.

Each call to ``dispatch(raw)`` spawns one ``asyncio.Task`` that runs
``_perceive`` (step-4 stub: passthrough text → DollEvent) then ``_respond``
(InnerVoice.recall + LLMAdapter.stream_completion, pushing chunks into the
event's ``response_sink`` and finally a ``None`` sentinel).

No worker, no queue. Multi-event concurrency is naturally bounded by the
underlying llama.cpp ``--parallel`` setting — not by this module.
"""

from __future__ import annotations

import asyncio
import logging

from dollos.events import DollEvent, RawEvent, UserTextEvent
from dollos.inner_voice import InnerVoice
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TurnEnd
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Spawns one asyncio.Task per RawEvent. No worker, no queue.

    Step 4 ships a stubbed ``_perceive`` that turns a UserTextEvent's text
    directly into a DollEvent.perception. Step 5 will replace this with
    ``InnerVoice.perceive(raw)``.
    """

    def __init__(
        self,
        *,
        adapter: LLMAdapter,
        inner_voice: InnerVoice,
        renderer: PromptRenderer,
        character_profile: str,
    ) -> None:
        self._adapter = adapter
        self._inner_voice = inner_voice
        self._renderer = renderer
        self._character_profile = character_profile
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def dispatch(self, raw: RawEvent) -> None:
        """Sync fan-out: spawn a task and return immediately."""
        if self._stopping:
            raise RuntimeError("EventDispatcher is stopping")
        task = asyncio.create_task(
            self._handle(raw), name=f"event-{type(raw).__name__}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        """Cancel all in-flight tasks; subsequent dispatch() raises."""
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _handle(self, raw: RawEvent) -> None:
        # Resolve sink BEFORE try/except. If the raw event has no associated
        # sink (programming bug — caller dispatched an unsupported type), we
        # have nowhere to push errors, so log and bail.
        try:
            sink = self._sink_of(raw)
        except TypeError:
            logger.exception("no sink for raw event %r", type(raw).__name__)
            return

        try:
            doll_event = await self._perceive(raw)
            await self._respond(doll_event, sink)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("dispatcher _handle error")
            sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
        finally:
            sink.put_nowait(None)

    async def _perceive(self, raw: RawEvent) -> DollEvent:
        # Step 4 stub: passthrough for UserTextEvent.
        # Step 5 will replace this body with:
        #     return await self._inner_voice.perceive(raw)
        if isinstance(raw, UserTextEvent):
            return DollEvent(perception=raw.text, raw=raw)
        raise TypeError(f"no stub perceive for {type(raw).__name__}")

    async def _respond(
        self,
        doll_event: DollEvent,
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        recall = await self._inner_voice.recall(doll_event.perception)
        system = self._renderer.render(
            "scaffolding", character=self._character_profile
        )
        prefill = f"{recall}DECISION: "
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=doll_event.perception,
            prefill=prefill,
        ):
            if chunk.text:
                sink.put_nowait(TextChunk(text=chunk.text))
            if chunk.done:
                break
        sink.put_nowait(TurnEnd())

    @staticmethod
    def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
        # Step 4: only UserTextEvent has a sink. Future RawEvent types may
        # have none (e.g. TimerFiredEvent — output via tool calls / IPC push).
        if isinstance(raw, UserTextEvent):
            return raw.response_sink
        raise TypeError(f"no sink for {type(raw).__name__}")
