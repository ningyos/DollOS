"""Distinctive marker prefixes for the [Self revision candidate] surfacing (spec §3.4).

Shared between the surfacing renderer (Task 8) and the echo-equivalence
normalizer (evolution._normalize_echo) so the two can never drift — an echoed
old/new block must strip exactly what the renderer prepended (A1
``_strip_incoming_tag`` analogue). The markers are the daemon's words, so they
are English (plan 2026-10-19 Task 5); the ⟪⟫ brackets keep them from ever
matching her own prose."""
OLD = "⟪current⟫"
NEW = "⟪proposed⟫"
ALL = (OLD, NEW)
