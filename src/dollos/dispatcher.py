"""EventDispatcher — fan-out raw events to concurrent tasks.

Step 6: _respond now feeds the big-model stream into ToolStreamParser
and dispatches each parsed <tool_call> dict to its pydantic-model tool's run().
The big model emits ONLY <tool_call> blocks after </think>; naked text
is dropped (DEBUG log) by the parser. Single-round per event — tool
results NOT cascaded back (cascade is step 7).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from memsearch import MemSearch
from pydantic import ValidationError

from dollos.events import DollEvent, RawEvent, UserTextEvent
from dollos.inner_voice import InnerVoice
from dollos.instinct import Instinct
from dollos.ipc.messages import ErrorMsg, ServerMessage, TurnEnd
from dollos.llm.adapter import LLMAdapter
from dollos.memory_writer import append_transcript
from dollos.prompts import PromptRenderer
from dollos.tool_parser import ToolStreamParser
from dollos.tools import TOOLS, ToolCtx

logger = logging.getLogger(__name__)

MAX_CASCADE_DEPTH = 50


@dataclass
class ToolCallFailure:
    """Tool call could not execute. Internal cascade primitive (not a RawEvent).

    Used by the inner cascade loop in _respond to build the next-iteration
    perception so Doll sees her own failed call and can self-correct.
    """

    tool_name: str
    error: str


class EventDispatcher:
    """Spawns one asyncio.Task per RawEvent. No worker, no queue."""

    def __init__(
        self,
        *,
        adapter: LLMAdapter,
        inner_voice: InnerVoice,
        instinct: Instinct,
        renderer: PromptRenderer,
        character_profile: str,
        memory_root: Path,
        memsearch: MemSearch,
        transcripts_root: Path,
    ) -> None:
        self._adapter = adapter
        self._inner_voice = inner_voice
        self._instinct = instinct
        self._renderer = renderer
        self._character_profile = character_profile
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._transcripts_root = transcripts_root
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in TOOLS
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def dispatch(self, raw: RawEvent) -> None:
        if self._stopping:
            raise RuntimeError("EventDispatcher is stopping")
        task = asyncio.create_task(
            self._handle(raw), name=f"event-{type(raw).__name__}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _handle(self, raw: RawEvent) -> None:
        try:
            sink = self._sink_of(raw)
        except TypeError:
            logger.exception("no sink for raw event %r", type(raw).__name__)
            return

        try:
            doll_event = await self._perceive(raw)
            summary = await self._instinct.process(doll_event)
            await self._respond(doll_event, summary, sink)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("dispatcher _handle error")
            sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
        finally:
            # Write user text AFTER the turn completes — avoids same-turn
            # recall self-matching (memsearch returning the just-written
            # user message as a hit on its own perception query).
            if isinstance(raw, UserTextEvent):
                try:
                    await append_transcript(
                        transcripts_root=self._transcripts_root,
                        memsearch=self._memsearch,
                        role="user",
                        text=raw.text,
                    )
                except Exception:
                    logger.exception("transcript append failed for UserTextEvent")
            sink.put_nowait(None)

    async def _perceive(self, raw: RawEvent) -> DollEvent:
        if isinstance(raw, UserTextEvent):
            return DollEvent(perception=raw.text, raw=raw)
        raise TypeError(f"no stub perceive for {type(raw).__name__}")

    async def _respond(
        self,
        doll_event: DollEvent,
        summary: str,
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        from dollos import dispatcher as _disp_mod

        iteration = 0
        while True:
            recall = await self._inner_voice.recall(doll_event.perception)
            system = self._renderer.render(
                "scaffolding", character=self._character_profile
            )
            state_block = f"STATE:\n{summary}\n\n" if summary else ""
            prefill = f"{state_block}{recall}DECISION: "

            parser = ToolStreamParser()
            ctx = ToolCtx(
                sink=sink,
                memory_root=self._memory_root,
                memsearch=self._memsearch,
                transcripts_root=self._transcripts_root,
            )
            fails: list[ToolCallFailure] = []

            async for chunk in self._adapter.stream_completion(
                system=system,
                user=doll_event.perception,
                prefill=prefill,
                tools=TOOLS,
            ):
                for call in parser.feed(chunk.text):
                    fail = await self._dispatch_tool_call(call, ctx)
                    if fail is not None:
                        fails.append(fail)
                if chunk.done:
                    break
            for call in parser.flush():
                fail = await self._dispatch_tool_call(call, ctx)
                if fail is not None:
                    fails.append(fail)

            if not fails:
                break

            iteration += 1
            if iteration > _disp_mod.MAX_CASCADE_DEPTH:
                sink.put_nowait(ErrorMsg(
                    message=(
                        f"cascade exceeded MAX_CASCADE_DEPTH "
                        f"({_disp_mod.MAX_CASCADE_DEPTH})"
                    )
                ))
                break

            doll_event = DollEvent(
                perception=self._format_fail_perception(fails, iteration),
                raw=doll_event.raw,
            )
            summary = await self._instinct.process(doll_event)

        sink.put_nowait(TurnEnd())

    @staticmethod
    def _format_fail_perception(
        fails: list[ToolCallFailure], iteration: int
    ) -> str:
        lines = [
            f"你 call 了 {f.tool_name} tool 失敗：{f.error}"
            for f in fails
        ]
        lines.append(f"（這是 thread 的第 {iteration} 次重試）")
        return "\n".join(lines)

    async def _dispatch_tool_call(
        self, call: dict, ctx: ToolCtx
    ) -> ToolCallFailure | None:
        name = call.get("name")
        if not isinstance(name, str):
            return ToolCallFailure(
                tool_name=str(name),
                error="missing or non-string 'name' field in tool_call",
            )
        tool_cls = self._tools_by_name.get(name)
        if tool_cls is None:
            logger.warning("unknown tool: %r", name)
            return ToolCallFailure(tool_name=name, error="unknown tool")
        try:
            tool = tool_cls.model_validate(call.get("arguments", {}))
        except ValidationError as e:
            logger.warning("tool args validation failed for %s: %s", name, e)
            return ToolCallFailure(
                tool_name=name, error=f"args validation: {e}"
            )
        try:
            await tool.run(ctx)
        except Exception as e:
            logger.exception("tool %s raised", name)
            ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
            return ToolCallFailure(
                tool_name=name, error=f"runtime error: {e}"
            )
        return None

    @staticmethod
    def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
        if isinstance(raw, UserTextEvent):
            return raw.response_sink
        raise TypeError(f"no sink for {type(raw).__name__}")
