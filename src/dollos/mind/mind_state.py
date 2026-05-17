"""MindState — single source of truth for Doll's continuous consciousness.

See docs/superpowers/specs/2026-05-16-persistent-mind-design.md.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


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
        "MonitorEnded", "ScheduledMoment", "Awoke",
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


def save_state(state: MindState, path: Path) -> None:
    """Save MindState to JSON file atomically.

    Uses atomic write via temporary file + rename to avoid partial writes on crash.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert deques to lists for JSON serialization
    state_dict = asdict(state)
    state_dict["recent_perceptions"] = list(state.recent_perceptions)
    state_dict["recent_outputs"] = list(state.recent_outputs)
    state_dict["recent_thoughts"] = list(state.recent_thoughts)

    # Convert dataclass instances to dicts for nested structures
    state_dict["mood"] = asdict(state.mood)
    state_dict["active_tasks"] = [asdict(t) for t in state.active_tasks]
    state_dict["pending_events"] = [asdict(e) for e in state.pending_events]
    state_dict["open_loops"] = [asdict(l) for l in state.open_loops]
    state_dict["recent_perceptions"] = [asdict(p) for p in state_dict["recent_perceptions"]]
    state_dict["recent_outputs"] = [asdict(o) for o in state_dict["recent_outputs"]]
    state_dict["recent_thoughts"] = [asdict(t) for t in state_dict["recent_thoughts"]]

    # Atomic write: write to temp, then rename
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(state_dict, f, indent=2)
        tmp_path.replace(path)
    except Exception as e:
        logger.error(f"Failed to save MindState to {path}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()


def load_state(path: Path) -> MindState:
    """Load MindState from JSON file with state refresh.

    Missing file returns fresh MindState.
    Malformed JSON logs error and returns fresh MindState.
    Refreshes session_started_at=time.time() on load.
    """
    path = Path(path)

    if not path.exists():
        return MindState()

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Malformed JSON in {path}: {e}")
        return MindState()
    except Exception as e:
        logger.error(f"Failed to load MindState from {path}: {e}")
        return MindState()

    # Reconstruct nested dataclasses
    try:
        mood = Mood(**data.get("mood", {}))
        active_tasks = [ActiveTask(**t) for t in data.get("active_tasks", [])]
        pending_events = [PendingEvent(**e) for e in data.get("pending_events", [])]
        open_loops = [OpenLoop(**l) for l in data.get("open_loops", [])]
        recent_perceptions = deque(
            [Perception(**p) for p in data.get("recent_perceptions", [])],
            maxlen=20
        )
        recent_outputs = deque(
            [OutputRecord(**o) for o in data.get("recent_outputs", [])],
            maxlen=15
        )
        recent_thoughts = deque(
            [Thought(**t) for t in data.get("recent_thoughts", [])],
            maxlen=10
        )

        state = MindState(
            mood=mood,
            focus=data.get("focus", "idle"),
            scratchpad=data.get("scratchpad", ""),
            active_tasks=active_tasks,
            pending_events=pending_events,
            open_loops=open_loops,
            recent_perceptions=recent_perceptions,
            recent_outputs=recent_outputs,
            recent_thoughts=recent_thoughts,
            last_user_at=data.get("last_user_at", 0.0),
            last_iter_at=data.get("last_iter_at", 0.0),
            iter_count=data.get("iter_count", 0),
            session_started_at=time.time(),  # REFRESH: new session
        )
        return state
    except Exception as e:
        logger.error(f"Failed to reconstruct MindState from {path}: {e}")
        return MindState()
