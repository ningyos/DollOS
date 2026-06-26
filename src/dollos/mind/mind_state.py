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


class MindStateLoadError(Exception):
    """Raised when a persisted MindState file is genuinely unreadable.

    Surface-not-blank policy: rather than silently returning a blank
    ``MindState()`` (total amnesia), a corrupt state file is quarantined and
    this exception is raised so startup halts loudly instead of presenting a
    fresh self as if nothing happened.
    """


def _coerce(cls, d: dict):
    """Field-tolerant reconstruct of a dataclass from a dict.

    Filters ``d`` to ``cls``'s declared dataclass fields before constructing,
    so additive schema drift (a future extra key inside a record) is tolerated
    rather than raising ``TypeError`` and blanking the whole self.
    """
    fields = cls.__dataclass_fields__
    filtered = {k: v for k, v in d.items() if k in fields}
    return cls(**filtered)


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
        "MonitorEnded", "ScheduledMoment", "Awoke", "ReflectionMoment",
        "Interrupted", "SafeModeEntered",
    ]
    t: float
    data: dict
    seq: int | None = None  # set by PerceptionQueue when WAL is active


@dataclass
class OutputRecord:
    t: float
    kind: str
    summary: str


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

    # Last N post-hoc REVIEW think-lines (metacognition surfaced back into the
    # prompt). Append-only ring buffer; see P2-capture spec §6.2.
    recent_reviews: deque[str] = field(default_factory=lambda: deque(maxlen=5))

    last_user_at: float = 0.0
    last_iter_at: float = 0.0
    iter_count: int = 0
    session_started_at: float = field(default_factory=time.time)

    # Read-only safe mode (spec §8.3): an explicit, announced, bounded-severity
    # boundary entered after repeated tool failures. NOT a fallback — the state
    # is surfaced loudly (persistent banner + help perception) and the tool set
    # is narrowed to read-only until a user turn clears it.
    safe_mode: bool = False
    safe_mode_reason: str = ""


def save_state(state: MindState, path: Path) -> bool:
    """Save MindState to JSON file atomically.

    Uses atomic write via temporary file + rename to avoid partial writes on crash.

    Returns ``True`` iff the temp file was written and atomically renamed into
    place. On any failure the temp file is cleaned up, the error is logged, and
    ``False`` is returned (no raise) — the caller decides what to do (e.g. skip
    WAL truncation so perceptions survive for replay). The daemon must not crash
    mid-turn on a failed save.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        # Convert deques to lists for JSON serialization
        state_dict = asdict(state)
        state_dict["recent_perceptions"] = list(state.recent_perceptions)
        state_dict["recent_outputs"] = list(state.recent_outputs)

        # Convert dataclass instances to dicts for nested structures
        state_dict["mood"] = asdict(state.mood)
        state_dict["active_tasks"] = [asdict(t) for t in state.active_tasks]
        state_dict["pending_events"] = [asdict(e) for e in state.pending_events]
        state_dict["open_loops"] = [asdict(l) for l in state.open_loops]
        state_dict["recent_perceptions"] = [asdict(p) for p in state_dict["recent_perceptions"]]
        state_dict["recent_outputs"] = [asdict(o) for o in state_dict["recent_outputs"]]
        state_dict["recent_reviews"] = list(state.recent_reviews)

        # Atomic write: write to temp, then rename
        with open(tmp_path, "w") as f:
            json.dump(state_dict, f, indent=2)
        tmp_path.replace(path)
        return True
    except Exception as e:
        logger.error(f"Failed to save MindState to {path}: {e}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False


def _quarantine_and_raise(path: Path, reason: str) -> None:
    """Move an unreadable state file aside and raise MindStateLoadError.

    Surface-not-blank: never silently return a blank self. The unreadable file
    is renamed to ``<name>.corrupt-<epoch>`` so it is preserved for inspection
    and a fresh state is not written over it on the next save.
    """
    quarantine = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    path.rename(quarantine)
    msg = (
        f"Failed to load MindState from {path}: {reason}. "
        f"Quarantined to {quarantine}."
    )
    logger.error(msg)
    raise MindStateLoadError(msg)


def load_state(path: Path) -> MindState:
    """Load MindState from JSON file with state refresh.

    Missing file is a genuine cold start → fresh ``MindState()``.
    Additive schema drift (extra top-level or nested keys) is tolerated
    field-tolerantly, preserving the rest of the self.
    Genuine corruption (malformed JSON or an unreconstructable record) is
    surfaced — the file is quarantined and ``MindStateLoadError`` is raised.
    Never blank-resets over a present-but-unreadable file.
    Refreshes session_started_at=time.time() on load.
    """
    path = Path(path)

    if not path.exists():
        return MindState()

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _quarantine_and_raise(path, f"malformed JSON ({e})")

    # Reconstruct nested dataclasses (field-tolerant for additive drift)
    try:
        mood = _coerce(Mood, data.get("mood", {}))
        active_tasks = [_coerce(ActiveTask, t) for t in data.get("active_tasks", [])]
        pending_events = [_coerce(PendingEvent, e) for e in data.get("pending_events", [])]
        open_loops = [_coerce(OpenLoop, l) for l in data.get("open_loops", [])]
        recent_perceptions = deque(
            [_coerce(Perception, p) for p in data.get("recent_perceptions", [])],
            maxlen=20
        )
        recent_outputs = deque(
            [_coerce(OutputRecord, o) for o in data.get("recent_outputs", [])],
            maxlen=15
        )
        recent_reviews = deque(data.get("recent_reviews", []), maxlen=5)

        state = MindState(
            mood=mood,
            focus=data.get("focus", "idle"),
            scratchpad=data.get("scratchpad", ""),
            active_tasks=active_tasks,
            pending_events=pending_events,
            open_loops=open_loops,
            recent_perceptions=recent_perceptions,
            recent_outputs=recent_outputs,
            recent_reviews=recent_reviews,
            last_user_at=data.get("last_user_at", 0.0),
            last_iter_at=data.get("last_iter_at", 0.0),
            iter_count=data.get("iter_count", 0),
            session_started_at=time.time(),  # REFRESH: new session
            safe_mode=data.get("safe_mode", False),
            safe_mode_reason=data.get("safe_mode_reason", ""),
        )
        return state
    except Exception as e:
        _quarantine_and_raise(path, f"unreconstructable state ({e})")
