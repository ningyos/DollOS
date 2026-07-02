"""Distinctive marker prefixes for the [人格演化候選] surfacing (spec §3.4).

Shared between the surfacing renderer (Task 8) and the echo-equivalence
normalizer (evolution._normalize_echo) so the two can never drift — an echoed
old/new block must strip exactly what the renderer prepended (A1
``_strip_incoming_tag`` analogue)."""
OLD = "【現行·舊】"
NEW = "【候選·新】"
ALL = (OLD, NEW)
