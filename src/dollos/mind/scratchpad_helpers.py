"""Scratchpad mutation helpers (cap checks + edit semantics).

These were methods on the old Scratchpad class; now they operate on
MindState.scratchpad directly. Same behavior, just different storage.
"""
from __future__ import annotations

HARD_CAP = 2000


def write(state, content: str) -> None:
    if len(content) > HARD_CAP:
        raise ValueError(
            f"scratchpad write exceeds {HARD_CAP} char cap "
            f"({len(content)} chars). Edit or Clear first."
        )
    state.scratchpad = content


def append(state, text: str) -> int:
    sep = "\n" if state.scratchpad else ""
    new_total = len(state.scratchpad) + len(sep) + len(text)
    if new_total > HARD_CAP:
        raise ValueError(
            f"scratchpad append would exceed {HARD_CAP} chars "
            f"({new_total} after append). Edit or Clear first."
        )
    state.scratchpad = state.scratchpad + sep + text
    return new_total


def edit(state, old: str, new: str) -> None:
    count = state.scratchpad.count(old)
    if count == 0:
        raise ValueError(f"old_string not found in scratchpad: {old!r}")
    if count > 1:
        raise ValueError(
            f"old_string appears {count} times — add more context to disambiguate"
        )
    new_content = state.scratchpad.replace(old, new, 1)
    if len(new_content) > HARD_CAP:
        raise ValueError(
            f"edit would push scratchpad to {len(new_content)} chars "
            f"(cap {HARD_CAP})."
        )
    state.scratchpad = new_content


def clear(state) -> None:
    state.scratchpad = ""
