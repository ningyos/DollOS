"""MindState: the single persistent state object of the mind loop."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class MindState:
    mood: str = "calm"
    focus: str = "idle"
    energy: float = 1.0
    active_tasks: list[dict] = field(default_factory=list)
    open_loops: list[dict] = field(default_factory=list)
    recent_perceptions: deque = field(default_factory=lambda: deque(maxlen=20))
    recent_thoughts: deque = field(default_factory=lambda: deque(maxlen=10))
    scratchpad: str = ""
    last_user_at: float = 0.0
    last_iter_at: float = 0.0
    iter_count: int = 0
    session_started_at: float = field(default_factory=time.time)
    sleep_until: float = 0.0  # if > now, extend idle interval

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["recent_perceptions"] = list(self.recent_perceptions)
        d["recent_thoughts"] = list(self.recent_thoughts)
        return d

    def persist(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), default=str, indent=2))

    @classmethod
    def load(cls, path: Path) -> "MindState":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        rp = data.pop("recent_perceptions", [])
        rt = data.pop("recent_thoughts", [])
        s = cls(**data)
        s.recent_perceptions = deque(rp, maxlen=20)
        s.recent_thoughts = deque(rt, maxlen=10)
        return s

    def open_loop(self, loop_id: str, desc: str) -> None:
        # idempotent — don't double-open
        for l in self.open_loops:
            if l["id"] == loop_id:
                l["desc"] = desc
                return
        self.open_loops.append({"id": loop_id, "desc": desc, "opened_at": time.time()})

    def close_loop(self, loop_id: str, outcome: str) -> bool:
        for i, l in enumerate(self.open_loops):
            if l["id"] == loop_id:
                self.open_loops.pop(i)
                return True
        return False
