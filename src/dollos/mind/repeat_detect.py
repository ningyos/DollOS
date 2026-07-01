"""Deterministic successful-repeat detector (spec §13.1 P6).

Every tool's ``run()`` calls ``_record(ctx, kind, summary)`` (``tools.py``),
appending an ``OutputRecord`` to ``state.recent_outputs`` — this already
covers every tool, not just the Speech-specific case
``mind_prompt._render_outputs_header`` special-cases separately.

This module is a pure, count-based detector over that history: no LLM
judgment, no side effects. It deliberately excludes ``kind == "Speech"`` —
Speech already has its own, differently-tuned (30s-window, not count-based)
warning; double-firing on the same signal would be redundant.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.mind.mind_state import OutputRecord

# Mirrors the existing SAFE_MODE_FAIL_THRESHOLD = 3 convention (mind_loop.py).
REPEAT_STREAK_THRESHOLD = 3


def detect_repeat_streak(
    outputs: Sequence["OutputRecord"],
    threshold: int = REPEAT_STREAK_THRESHOLD,
) -> tuple[str, str, int] | None:
    """Detect a trailing run of identical (kind, summary) OutputRecords.

    Walks ``outputs`` from the end while both ``kind`` and ``summary`` equal
    the last entry's. Returns ``(kind, summary, count)`` when the run length
    is ``>= threshold``, else ``None``.

    Excludes Speech entirely: if the last entry's kind is ``"Speech"``,
    returns ``None`` immediately regardless of run length — Speech has its
    own separate, already-shipped warning (see ``mind_prompt``) and must not
    double-fire here.
    """
    items = list(outputs)
    if not items:
        return None
    last = items[-1]
    if last.kind == "Speech":
        return None
    count = 0
    for o in reversed(items):
        if o.kind == last.kind and o.summary == last.summary:
            count += 1
        else:
            break
    if count >= threshold:
        return (last.kind, last.summary, count)
    return None
