"""Event types — two-tier model.

RawEvent: structured event from a source (IPC text, voice, timer, ...).
DollEvent: natural-language perception emitted by Inner Voice's perceive(),
           consumed by the big LLM as the `user` role.

Step 4 ships RawEvent + UserTextEvent + DollEvent dataclasses. The
RawEvent → DollEvent conversion is stubbed (passthrough). Step 5 will
replace the stub with InnerVoice.perceive().
"""

from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass

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
class DollEvent:
    """Natural-language perception consumed by the big LLM as `user` role.

    perception: free-form natural language including source semantics
        ("主人在手機上對我說 X", "鬧鐘響了", "drone 回報：...").
    raw: back-reference to the RawEvent for engineering routing
        (response_sink, source metadata). Doll itself does not see this.
    """

    perception: str
    raw: RawEvent
