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
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from memsearch import MemSearch
from pydantic import ValidationError

from dollos.cascade_log import CascadeLogger
from dollos.character import Identity
from dollos.events import (
    DailyPlanEvent,
    DiaryEvent,
    DollEvent,
    MonitorExitedEvent,
    MonitorTriggeredEvent,
    RawEvent,
    ScheduledEvent,
    ShellResultEvent,
    SubagentResultEvent,
    UserTextEvent,
)

if TYPE_CHECKING:
    from dollos.monitor_runner import MonitorRunner
from dollos.inner_voice import InnerVoice
from dollos.instinct import Instinct
from dollos.ipc.messages import ErrorMsg, ServerMessage, TurnEnd
from dollos.llm.adapter import LLMAdapter
from dollos.llm.templates import build_qwen3_think_tool_grammar
from dollos.memory_writer import append_transcript
from dollos.prompts import PromptRenderer
from dollos.shell_runner import ShellRunner
from dollos.subagent import SubagentRunner
from dollos.tool_parser import ToolStreamParser
from dollos.tools import TOOLS, ToolCtx

logger = logging.getLogger(__name__)


# Event types that demand serialized handling — racing cascades over the same
# mood / rolling buffer / cascade log are observable bugs (gap #2). Internal
# async results (SubagentResultEvent) stay parallel: they're "result of work
# Doll started" so queueing them just makes the user wait artificially.
SERIALIZE_TYPES: tuple[type, ...] = (
    UserTextEvent, ScheduledEvent, DailyPlanEvent, DiaryEvent,
    MonitorTriggeredEvent, MonitorExitedEvent,
)


_WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def _period_of_day(hour: int) -> str:
    if hour < 5:
        return "深夜"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 18:
        return "下午"
    return "晚上"


_MOOD_LINE_RE = re.compile(r"^MOOD:\s*(.+)$", re.MULTILINE)


def _parse_last_mood(messages: list[dict]) -> str | None:
    """Find the last assistant message; extract MOOD: line value if present."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            m = _MOOD_LINE_RE.search(content)
            if m:
                return m.group(1).strip()
            return None
    return None


def _format_now(now: datetime) -> str:
    return (
        f"[Now]\n"
        f"{now:%Y-%m-%d %H:%M:%S} "
        f"{_WEEKDAYS[now.weekday()]}{_period_of_day(now.hour)}\n\n"
    )


def _format_active_monitors_block(snapshot: list[dict]) -> str:
    """Render the [Active monitors] block from MonitorRunner.active_state()."""
    if not snapshot:
        return ""
    lines: list[str] = ["[Active monitors]"]
    for s in snapshot:
        match_part = (
            f" (match: {s['match_regex']!r})" if s["match_regex"] else ""
        )
        rl = s["rate_limit_s"]
        if rl > 0:
            sup = s["suppressed_in_window"]
            if sup > 0:
                rl_part = f" [rate-limit: {rl}s, suppressed {sup} in last {rl}s]"
            else:
                rl_part = f" [rate-limit: {rl}s]"
        else:
            rl_part = ""
        lines.append(
            f"- {s['monitor_id']}: {s['command']}{match_part}{rl_part}"
        )
    return "\n".join(lines) + "\n\n"


@dataclass
class ToolResult:
    """Tool execution result. Internal cascade primitive (not a RawEvent).

    success=False: mechanical fail (validation / unknown / runtime exception).
    success=True:  ran cleanly. detail = the str returned by Tool.run().
                   May be empty string (Tool ran but had no content to return).

    Failures always cascade (Doll should fix). Successes cascade iff
    Tool.run() returned a str (not None) — i.e., the tool author opted in.
    """

    tool_name: str
    success: bool
    detail: str


class _NoOpCascadeLogger:
    """No-op CascadeLogger used as default when none is wired (tests)."""

    def start_turn(self) -> str:
        return "noop"

    def log_iter(self, **_kwargs) -> None:
        return None


class EventDispatcher:
    """Spawns one asyncio.Task per RawEvent. No worker, no queue."""

    def __init__(
        self,
        *,
        adapter: LLMAdapter,
        inner_voice: InnerVoice,
        instinct: Instinct,
        renderer: PromptRenderer,
        identity: Identity,
        memory_root: Path,
        memsearch: MemSearch,
        transcripts_root: Path,
        subagent_runner: SubagentRunner | None = None,
        cascade_logger: CascadeLogger | None = None,
        shell_runner: ShellRunner | None = None,
        monitor_runner: "MonitorRunner | None" = None,
    ) -> None:
        self._adapter = adapter
        self._inner_voice = inner_voice
        self._instinct = instinct
        self._renderer = renderer
        self._identity = identity
        self._memory_root = memory_root
        self._memsearch = memsearch
        self._transcripts_root = transcripts_root
        self._subagent_runner = subagent_runner
        self._cascade_logger = cascade_logger or _NoOpCascadeLogger()
        self._shell_runner = shell_runner
        self._monitor_runner = monitor_runner
        self._tools_by_name: dict[str, type] = {
            cls.__name__: cls for cls in TOOLS
        }
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        # Phase 1 schedule: serialize user-facing events so racing cascades
        # don't interleave mood / rolling / cascade-log writes. Pending events
        # are surfaced in the active cascade's next-iter `[Pending events]`
        # block so Doll can choose whether to wrap up early.
        self._active_cascade: asyncio.Task[None] | None = None
        self._pending: list[RawEvent] = []
        self._parallel_tasks: set[asyncio.Task[None]] = set()
        # Rolling buffer of per-cascade first-person summaries. Daemon-lifetime;
        # surfaced as `[Recent activity]` block on each turn's first user message.
        # Each entry is (timestamp, summary) — timestamp captured at compact time.
        self._rolling: list[tuple[datetime, str]] = []
        # Doll's current mood — natural-language sentence updated each cascade.
        # Default at daemon start; evolves via `Instinct.compact_cascade`.
        # Surfaces as `[Mood]` block on each turn's first user message.
        self._current_mood: str = "平靜，剛醒來"

    def _format_mood(self) -> str:
        return f"[Mood]\n{self._current_mood}\n\n"

    async def _append_mood(self, mood: str) -> None:
        path = self._memory_root / "mood" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H:%M:%S")
        with path.open("a") as f:
            f.write(f"## ({timestamp}) {mood}\n")
        await self._memsearch.index_file(path)

    def _format_recent_activity(self) -> str:
        if not self._rolling:
            return ""
        today = date.today()
        lines = []
        for ts, summary in self._rolling:
            if ts.date() == today:
                prefix = f"{ts:%H:%M:%S}"
            else:
                prefix = f"{ts:%Y-%m-%d %H:%M:%S}"
            lines.append(f"- {prefix} {summary}")
        return "[Recent activity]\n" + "\n".join(lines) + "\n\n"

    def dispatch(self, raw: RawEvent) -> None:
        if self._stopping:
            raise RuntimeError("EventDispatcher is stopping")
        if isinstance(raw, SERIALIZE_TYPES):
            if (
                self._active_cascade is not None
                and not self._active_cascade.done()
            ):
                self._pending.append(raw)
                return
            task = asyncio.create_task(
                self._handle(raw), name=f"event-{type(raw).__name__}"
            )
            self._active_cascade = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(self._on_cascade_done)
        else:
            task = asyncio.create_task(
                self._handle(raw), name=f"event-{type(raw).__name__}"
            )
            self._parallel_tasks.add(task)
            self._tasks.add(task)
            task.add_done_callback(self._parallel_tasks.discard)
            task.add_done_callback(self._tasks.discard)

    def _on_cascade_done(self, task: asyncio.Task[None]) -> None:
        # Pop the next serialized event off the pending queue and start it.
        # Iterate in case a stray event slipped in for an already-stopping
        # dispatcher — we just drop those.
        while self._pending:
            if self._stopping:
                self._pending.clear()
                break
            nxt = self._pending.pop(0)
            new_task = asyncio.create_task(
                self._handle(nxt), name=f"event-{type(nxt).__name__}"
            )
            self._active_cascade = new_task
            self._tasks.add(new_task)
            new_task.add_done_callback(self._tasks.discard)
            new_task.add_done_callback(self._on_cascade_done)
            return
        self._active_cascade = None

    async def stop(self) -> None:
        self._stopping = True
        # Drop anything still pending; nothing will dispatch them now.
        self._pending.clear()
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._active_cascade = None

    def _format_pending(self) -> str:
        if not self._pending:
            return ""
        lines: list[str] = []
        for ev in self._pending:
            if isinstance(ev, UserTextEvent):
                lines.append(f"- UserTextEvent: 主人說「{ev.text}」")
            elif isinstance(ev, ScheduledEvent):
                lines.append(
                    f"- ScheduledEvent: {ev.entry_time:%H:%M:%S} {ev.intent}"
                )
            elif isinstance(ev, DailyPlanEvent):
                lines.append("- DailyPlanEvent: 該規劃今天時程")
            elif isinstance(ev, DiaryEvent):
                lines.append("- DiaryEvent: 該寫今日 diary")
            else:  # defensive — unknown serialize type
                lines.append(f"- {type(ev).__name__}")
        return "[Pending events]\n" + "\n".join(lines) + "\n\n"

    async def _handle(self, raw: RawEvent) -> None:
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
        if isinstance(raw, DiaryEvent):
            perception = (
                "今天該寫日記了。回顧今天發生的事跟你的感受，"
                "用 WriteDiary tool 寫一段反思。誠實寫，不需要表演。"
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, SubagentResultEvent):
            perception = (
                "你派出的 subagent 回來了：\n"
                f"- task: {raw.task}\n"
                f"- status: {raw.status}\n"
                f"- summary: {raw.summary}\n"
                f"- details: {raw.details}"
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, ShellResultEvent):
            perception = (
                "你執行的 shell 命令回來了：\n"
                f"- command: {raw.command}\n"
                f"- status: {raw.status} (exit {raw.exit_code})\n"
                f"- output:\n{raw.output}"
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, MonitorTriggeredEvent):
            extra = (
                f"（過去 window 內被 rate-limit 壓住的命中數: {raw.suppressed_count}）"
                if raw.suppressed_count > 0 else ""
            )
            perception = (
                f"monitor {raw.monitor_id} 觸發：\n"
                f"- command: {raw.command}\n"
                f"- line: {raw.line}\n"
                f"{extra}".rstrip()
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, MonitorExitedEvent):
            perception = (
                f"monitor {raw.monitor_id} 結束（status={raw.status}, "
                f"exit={raw.exit_code}, 共觸發 {raw.total_matched} 行）：\n"
                f"- command: {raw.command}"
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, DailyPlanEvent):
            perception = (
                "早安。要規劃今天的時程。看一下昨天的 schedule（Recall 找）"
                "跟最近的 diary，決定今天要主動做什麼事，呼叫 WriteSchedule "
                "(entries=[...]) 寫下來。"
            )
            return DollEvent(perception=perception, raw=raw)
        if isinstance(raw, ScheduledEvent):
            perception = (
                f"你早上計劃了 {raw.entry_time:%H:%M:%S} 要做：\n"
                f"{raw.intent}\n\n"
                f"該開始了。"
            )
            return DollEvent(perception=perception, raw=raw)
        raise TypeError(f"no stub perceive for {type(raw).__name__}")

    async def _respond(
        self,
        doll_event: DollEvent,
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        grammar = build_qwen3_think_tool_grammar(TOOLS)

        # Per-turn one-time setup: recall + scaffolding (system) + skills
        # discovery. The cascade preserves history within the turn via the
        # `messages` list; `[Memory context]` only appears in the first
        # user message, never re-injected.
        recall_text = await self._inner_voice.recall(doll_event.perception)
        recent_activity = self._format_recent_activity()
        if recall_text:
            memory_block = f"[Memory context]\n{recall_text}\n\n"
        else:
            memory_block = "[Memory context]\n(no relevant memory)\n\n"
        if self._monitor_runner is not None:
            active_monitors_block = _format_active_monitors_block(
                self._monitor_runner.active_state()
            )
        else:
            active_monitors_block = ""
        first_user = (
            _format_now(datetime.now())
            + self._format_mood()
            + self._format_pending()
            + active_monitors_block
            + recent_activity
            + memory_block
            + f"[Message]\n{doll_event.perception}"
        )
        messages: list[dict] = [{"role": "user", "content": first_user}]

        skills_dir = self._memory_root / "skills"
        if skills_dir.exists():
            available_skills = sorted(p.stem for p in skills_dir.glob("*.md"))
        else:
            available_skills = []
        system = self._renderer.render(
            "scaffolding",
            identity=self._identity,
            available_skills=available_skills,
        )

        # Same-tool consecutive-failure tracker.
        consecutive_fails: dict[str, int] = {}
        last_failed_tool: str | None = None
        turn_id = self._cascade_logger.start_turn()
        iter_num = 0
        while True:
            iter_num += 1
            iter_start = time.monotonic()
            # On iter >= 2 we may have new pending events that arrived
            # while the previous iter ran. Inject a fresh `[Pending events]`
            # block as a standalone user message so Doll can react before
            # her next think (gap #4 — re-check per iter).
            if iter_num > 1:
                pending_block = self._format_pending()
                if pending_block:
                    messages.append({
                        "role": "user",
                        "content": pending_block.rstrip(),
                    })
                if self._monitor_runner is not None:
                    active_block = _format_active_monitors_block(
                        self._monitor_runner.active_state()
                    )
                    if active_block:
                        messages.append({
                            "role": "user",
                            "content": active_block.rstrip(),
                        })
            parser = ToolStreamParser()
            ctx = ToolCtx(
                sink=sink,
                memory_root=self._memory_root,
                memsearch=self._memsearch,
                transcripts_root=self._transcripts_root,
                subagent_runner=self._subagent_runner,
                shell_runner=self._shell_runner,
                monitor_runner=self._monitor_runner,
            )
            results: list[ToolResult] = []
            assistant_buf: list[str] = []
            parsed_tool_calls: list[dict] = []

            async for chunk in self._adapter.stream_messages(
                system=system,
                messages=messages,
                tools=TOOLS,
                max_tokens=4096,
                grammar=grammar,
            ):
                if chunk.text:
                    assistant_buf.append(chunk.text)
                for call in parser.feed(chunk.text):
                    parsed_tool_calls.append(call)
                    result = await self._dispatch_tool_call(call, ctx)
                    if result is not None:
                        results.append(result)
                if chunk.done:
                    break
            for call in parser.flush():
                parsed_tool_calls.append(call)
                result = await self._dispatch_tool_call(call, ctx)
                if result is not None:
                    results.append(result)

            # Append the model's full raw emit as the assistant turn.
            messages.append({
                "role": "assistant",
                "content": "".join(assistant_buf),
            })

            duration_ms = int((time.monotonic() - iter_start) * 1000)
            self._cascade_logger.log_iter(
                turn_id=turn_id,
                iter=iter_num,
                assistant_text="".join(assistant_buf),
                tool_calls=parsed_tool_calls,
                results=results,
                duration_ms=duration_ms,
            )

            if not results:
                break

            # Append a tool_response user message for each tool result.
            for r in results:
                detail = r.detail if r.detail else "(no output)"
                messages.append({
                    "role": "user",
                    "content": f"<tool_response>\n{detail}\n</tool_response>",
                })

            # Update same-tool consecutive-failure tracker.
            for r in results:
                if r.success:
                    consecutive_fails.clear()
                    last_failed_tool = None
                else:
                    if r.tool_name == last_failed_tool:
                        consecutive_fails[r.tool_name] = (
                            consecutive_fails.get(r.tool_name, 1) + 1
                        )
                    else:
                        last_failed_tool = r.tool_name
                        consecutive_fails = {r.tool_name: 1}

            stuck_tool = next(
                (n for n, c in consecutive_fails.items() if c >= 3),
                None,
            )
            if stuck_tool is not None:
                sink.put_nowait(ErrorMsg(
                    message=(
                        f"cascade aborted: 連續 3 次 {stuck_tool} tool 失敗，"
                        f"停下來換思路。"
                    )
                ))
                break

        # Parse mood from last assistant message's MOOD: line (think field).
        # Big model writes mood in <think> as part of every cascade iteration;
        # we adopt the latest one.
        new_mood = _parse_last_mood(messages)
        if new_mood:
            self._current_mood = new_mood
            await self._append_mood(new_mood)

        # Compact the cascade into a 1-sentence summary; append to rolling
        # buffer for `[Recent activity]` block on next turn. Runs regardless
        # of cascade exit reason (natural / same-tool-abort).
        try:
            summary = await self._instinct.compact_cascade(
                perception=doll_event.perception,
                cascade_messages=messages,
            )
            if summary:
                self._rolling.append((datetime.now(), summary))
        except Exception:
            logger.exception("compact_cascade failed; rolling unchanged")

        sink.put_nowait(TurnEnd())

    async def _dispatch_tool_call(
        self, call: dict, ctx: ToolCtx
    ) -> ToolResult | None:
        """Execute a tool call. Returns ToolResult if cascade-worthy, None otherwise.

        Returns None when:
          - tool.run() returned None (side-effect tool, no cascade)
        Returns ToolResult when:
          - validation/unknown error (success=False, error in detail)
          - runtime exception (success=False, error in detail) — also pushes ErrorMsg to sink
          - tool.run() returned str (success=True, str in detail; may be empty)
        """
        name = call.get("name")
        if not isinstance(name, str):
            return ToolResult(
                tool_name=str(name), success=False,
                detail="missing or non-string 'name' field in tool_call",
            )
        tool_cls = self._tools_by_name.get(name)
        if tool_cls is None:
            logger.warning("unknown tool: %r", name)
            return ToolResult(tool_name=name, success=False, detail="unknown tool")
        try:
            tool = tool_cls.model_validate(call.get("arguments", {}))
        except ValidationError as e:
            logger.warning("tool args validation failed for %s: %s", name, e)
            return ToolResult(
                tool_name=name, success=False, detail=f"args validation: {e}"
            )
        try:
            returned = await tool.run(ctx)
        except Exception as e:
            logger.exception("tool %s raised", name)
            ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
            return ToolResult(
                tool_name=name, success=False, detail=f"runtime error: {e}"
            )
        if returned is None:
            return None
        return ToolResult(tool_name=name, success=True, detail=returned)

    @staticmethod
    def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
        if isinstance(
            raw,
            (
                UserTextEvent,
                DiaryEvent,
                SubagentResultEvent,
                ShellResultEvent,
                ScheduledEvent,
                DailyPlanEvent,
                MonitorTriggeredEvent,
                MonitorExitedEvent,
            ),
        ):
            return raw.response_sink
        raise TypeError(f"no sink for {type(raw).__name__}")
