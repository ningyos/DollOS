"""慢變演化 evidence layer — append-only event log (spec 2026-07-02 §3.2).

Records every self_profile mutation (tombstones included) and, in Plan 3,
the evolution-machinery events. Append-only; never FTS-indexed; never
injected into any prompt block. Pure module, no LLM — mirror of
self_profile.py's standalone, unit-testable style.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def log_event(path: Path, *, kind: str, **fields) -> None:
    """Append one event line (adds ``ts``). RAISES OSError on IO failure —
    the caller decides the swallow policy (pin events: swallow loudly;
    evolution events: abort — spec §3.2 write-ordering rules)."""
    event = {"ts": time.time(), "kind": kind, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()


def last_pin_turn(path: Path, *, section: str, text: str) -> int | None:
    """Outer-turn number of the most recent ``pin_add``/``pin_reconfirm`` for
    this exact section+text; None if never logged. Backward scan of the small
    jsonl file — deliberately not an in-memory map, so the cross-turn
    reconfirm rule survives restarts (spec §3.2)."""
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # torn tail line — tolerate, keep scanning
        if (ev.get("kind") in ("pin_add", "pin_reconfirm")
                and ev.get("section") == section and ev.get("text") == text):
            turn = ev.get("turn")
            return int(turn) if isinstance(turn, int) else None
    return None
