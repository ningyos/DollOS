"""Event types — two-tier model.

RawEvent: structured event from a source (IPC text, voice, timer, ...).
DollEvent: natural-language perception consumed by the big LLM as the
           `user` role.
"""

from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass
from datetime import time
from typing import Literal

from dollos.ipc.messages import ServerMessage


class RawEvent(ABC):  # noqa: B024 — marker base for future event subclasses
    """Structured event from a source. Future subclasses: VoiceInputEvent,
    TimerFiredEvent, ToolResultEvent, DroneResultEvent, ...
    """


@dataclass
class UserTextEvent(RawEvent):
    """Text typed by the user via IPC.

    response_sink: per-event queue. Dispatcher pushes ServerMessage objects
    for streaming back to the IPC handler, then ``None`` sentinel.
    """

    text: str
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class DiaryEvent(RawEvent):
    """Scheduled trigger for Doll to write today's diary.

    Has no user-facing sink — the daemon drains internally. Dispatcher's
    _perceive synthesizes a "write today's diary" perception so Doll wakes
    and calls the WriteDiary tool.
    """

    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class SubagentResultEvent(RawEvent):
    """A subagent task finished — result re-enters the event queue.

    Fired by SubagentRunner once the spawned worker either calls Report,
    times out, errors, or ends without reporting. The dispatcher renders
    this into a perception so Doll's main cascade fires for it.

    response_sink: forwarded from the originating SpawnSubagent call so
        the result-turn streams back to the same client. May reference a
        sink whose original consumer has already disconnected (see plan
        §Risks); push attempts on a dead queue are tolerated by asyncio.

    details: preview head of full details (~SUBAGENT_PREVIEW_LINES lines).
    details_output_id: ToolOutputStore key for the full details text;
        None on error paths where no details were captured.
    details_line_count: total line count of the full details text.
    """

    subagent_id: str
    task: str
    status: Literal["ok", "incomplete", "timeout", "error", "no_report"]
    summary: str   # unchanged — subagent's structured Report.summary (short by design)
    details: str   # preview head of full details (~SUBAGENT_PREVIEW_LINES lines)
    details_output_id: str | None   # None on error before any details captured
    details_line_count: int
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class ShellResultEvent(RawEvent):
    """A backgrounded Shell command completed — result re-enters event queue.

    Fired by ShellRunner when the spawned proc either exits, times out, or
    raises. Dispatcher renders this into a perception so Doll's cascade fires
    for it — same pattern as SubagentResultEvent.

    Status semantics:
        ok      — proc exited 0
        nonzero — proc exited with non-zero code (output still meaningful)
        timeout — wall-clock timeout reached; proc was killed
        error   — runner-level exception (spawn failure, decode failure)
    """

    command: str
    status: Literal["ok", "nonzero", "timeout", "error"]
    exit_code: int | None
    output: str   # preview head only (~10 lines)
    output_id: str | None   # None only if runner-level error before any output captured
    line_count: int   # total lines of the full output
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class MonitorTriggeredEvent(RawEvent):
    """A monitor command emitted a matched stdout line.

    Fired by MonitorRunner when a watched process emits a line that
    matches the monitor's `match_regex` (or any line if regex is None),
    subject to rate-limit. Dispatcher renders this into a perception so
    Doll's cascade fires for it.

    suppressed_count: when > 0, this fire represents one matched line
        plus N suppressed-by-rate-limit lines in the prior window. Doll
        sees the number in the perception to gauge frequency.
    """

    monitor_id: str
    command: str
    line: str
    suppressed_count: int
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class MonitorExitedEvent(RawEvent):
    """A monitor's command exited (naturally or via RemoveMonitor).

    Always fired exactly once per monitor, regardless of rate-limit.
    `status`: 'natural' if the proc exited on its own, 'removed' if
    RemoveMonitor killed it, 'error' if the runner raised.
    """

    monitor_id: str
    command: str
    status: Literal["natural", "removed", "error"]
    exit_code: int | None
    total_matched: int
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class ScheduledEvent(RawEvent):
    """Fired by the daemon scheduler when a scheduled entry's time arrives.

    ``entry_time`` and ``intent`` come straight from the entry Doll wrote
    via the WriteSchedule tool. ``response_sink`` routes Doll's cascade
    output back to whatever client is currently connected (or a dummy
    queue if none).
    """

    entry_time: time
    intent: str
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class DailyPlanEvent(RawEvent):
    """Fired on first WS connection of the day if no schedule exists yet.

    Also fireable manually for replan in later phases. Carries no data —
    the perception tells Doll to look at yesterday + recent diary and call
    ``WriteSchedule``.
    """

    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class DollEvent:
    """Natural-language perception consumed by the big LLM as `user` role.

    perception: free-form natural language including source semantics
        ("主人在手機上對我說 X", "鬧鐘響了", "drone 回報：...").
    raw: back-reference to the RawEvent for engineering routing
        (response_sink, source metadata). Doll itself does not see this.
    """

    perception: str
    raw: RawEvent
