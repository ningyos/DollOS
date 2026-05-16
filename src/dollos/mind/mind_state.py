"""MindState — single source of truth for Doll's continuous consciousness.

See docs/superpowers/specs/2026-05-16-persistent-mind-design.md.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Mood:
    """Doll's emotional state — a natural-language sentence."""
    emotion: str = "平靜"
    reason: str = ""


@dataclass
class ActiveTask:
    task_id: str
    kind: Literal["shell", "subagent", "monitor"]
    summary: str
    started_at: float

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at


@dataclass
class PendingEvent:
    fire_at: float
    summary: str


@dataclass
class OpenLoop:
    id: str
    desc: str
    opened_at: float


@dataclass
class Perception:
    kind: Literal[
        "UserSpoke", "ToolResultArrived", "MonitorFired",
        "MonitorEnded", "ScheduledMoment", "IdleTick", "Awoke",
    ]
    t: float
    data: dict


@dataclass
class OutputRecord:
    t: float
    kind: str
    summary: str


@dataclass
class Thought:
    t: float
    text: str


@dataclass
class MindState:
    mood: Mood = field(default_factory=Mood)
    focus: str = "idle"
    energy: float = 1.0
    scratchpad: str = ""

    active_tasks: list[ActiveTask] = field(default_factory=list)
    pending_events: list[PendingEvent] = field(default_factory=list)
    open_loops: list[OpenLoop] = field(default_factory=list)

    recent_perceptions: deque[Perception] = field(default_factory=lambda: deque(maxlen=20))
    recent_outputs: deque[OutputRecord] = field(default_factory=lambda: deque(maxlen=15))
    recent_thoughts: deque[Thought] = field(default_factory=lambda: deque(maxlen=10))

    last_user_at: float = 0.0
    last_iter_at: float = 0.0
    iter_count: int = 0
    session_started_at: float = field(default_factory=time.time)
