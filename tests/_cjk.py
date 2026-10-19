"""Find CJK text — for the private-prompt language guard (plan 2026-10-19 Task 1).

CJK here means ideographs (Extension A, the unified block, compatibility
ideographs) and full-width punctuation/forms (。，：「」『』【】 …).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_CJK_RUN = re.compile("[㐀-鿿豈-﫿　-〿＀-￯]+")
_CONTEXT_CHARS = 30
_MAX_REPORTED = 40


class ForeignCJK(AssertionError):
    """The text carries CJK outside the allowed strings."""


def cjk_runs(text: str) -> list[str]:
    """Every run of consecutive CJK characters in ``text``, in order."""
    return _CJK_RUN.findall(text)


def assert_only_allowed(text: str, allowed: Iterable[str]) -> None:
    """Raise ``ForeignCJK`` unless every CJK character in ``text`` belongs to
    an occurrence of one of the ``allowed`` strings.

    Each allowed string is taken out whole (longest first) before looking:
    a run that is merely a substring of an allowed string (主人 inside the
    example note) still counts. The message shows each run with the text
    around it."""
    masked = text
    for s in sorted({a for a in allowed if a}, key=len, reverse=True):
        masked = masked.replace(s, "\0" * len(s))
    found = list(_CJK_RUN.finditer(masked))
    if not found:
        return
    lines = []
    for m in found[:_MAX_REPORTED]:
        lo = max(0, m.start() - _CONTEXT_CHARS)
        hi = min(len(text), m.end() + _CONTEXT_CHARS)
        lines.append(f"  {m.group()!r} in {text[lo:hi]!r}")
    if len(found) > _MAX_REPORTED:
        lines.append(f"  … and {len(found) - _MAX_REPORTED} more")
    raise ForeignCJK(
        f"{len(found)} CJK run(s) outside the allowed strings:\n" + "\n".join(lines)
    )
