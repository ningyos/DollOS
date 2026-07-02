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


def read_events(path: Path) -> list[dict]:
    """All parseable event dicts, oldest→newest. Torn tail lines are skipped
    (same tolerance as ``last_pin_turn``). Missing file → []. The jsonl file is
    small (weeks of events); a full read per call is fine."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest_adopt(path: Path) -> dict | None:
    """The most recent ``evo_adopt`` event, or None. Backward scan (small file,
    restart-safe — the log is the audit source of truth, spec §5)."""
    for ev in reversed(read_events(path)):
        if ev.get("kind") == "evo_adopt":
            return ev
    return None


def sanctioned_text(path: Path) -> str | None:
    """The last sanctioned ``current_self`` text := latest ``evo_adopt``'s
    ``text`` (spec §5). None before the first adoption (pack-only)."""
    ev = latest_adopt(path)
    return ev.get("text") if ev is not None else None


def generation(path: Path) -> int:
    """Persona generation := count of ``evo_adopt`` events (spec §3.5). 0 =
    pack-only, pre-first-adoption."""
    return sum(1 for ev in read_events(path) if ev.get("kind") == "evo_adopt")


def latest_external_edit_text(path: Path) -> str | None:
    """``text`` of the most recent ``external_edit`` event that carried one
    (the last-observed external edit — used by the tripwire to avoid
    per-turn re-detection of an already-logged divergence, spec §5). A
    mechanical-fail ``external_edit(reason=...)`` line carries no ``text`` and
    is skipped."""
    for ev in reversed(read_events(path)):
        if ev.get("kind") == "external_edit" and ev.get("text") is not None:
            return ev["text"]
    return None
