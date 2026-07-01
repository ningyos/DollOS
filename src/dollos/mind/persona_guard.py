"""Mechanical persona-taboo enforcement (spec 2026-07-01 persona-hardening §2).

A weak/pressured local model can and does ignore prose-only constraints
(`character.py`'s `taboos` field has zero code-level consumer). This module
is the code-level consumer: a pure, no-I/O detector over a pack's declared
`Enforcement` rules. It does not block or rewrite anything — callers
(`mind_loop.py`) use it to *announce* violations, not silently censor them.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dollos.character import Enforcement

# ASCII "!" and fullwidth "！" are treated as the same exclaim character for
# run-length purposes — a mixed run like "!！" still counts as length 2.
_EXCLAIM_RUN_RE = re.compile(r"[!！]+")


def check_persona_violations(text: str, rules: "Enforcement") -> list[str]:
    """Return human-readable violation descriptions, or [] if clean.

    Checks each ``rules.banned_substrings`` entry via a case-sensitive
    literal ``in`` check, and — if ``rules.max_exclaim_run`` is set — flags
    any run of ``!``/``！`` longer than that threshold. Pure function, no I/O.
    """
    violations: list[str] = []

    for banned in rules.banned_substrings:
        if banned in text:
            violations.append(f"banned substring found: {banned!r}")

    if rules.max_exclaim_run is not None:
        for match in _EXCLAIM_RUN_RE.finditer(text):
            if len(match.group()) > rules.max_exclaim_run:
                violations.append(
                    f"exclaim run of {len(match.group())} exceeds max "
                    f"{rules.max_exclaim_run}: {match.group()!r}"
                )

    return violations
