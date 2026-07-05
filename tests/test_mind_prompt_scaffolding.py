"""P1c Task 6: L2 external-situation scaffolding nudge.

Once a turn is admitted past the code-side gate (Tasks 1-5, the MAIN
defense against 亂回), Doll can still choose Say or silence (the cascade
emits 0..N actions). This is the secondary, DESCRIPTIVE nudge — narration
of the situation, not a command — so silence reads as a normal option on
a public turn: she's overhearing a lot that isn't addressed to her.

Only on ``origin_tier == "external_public"`` (a stranger in a public
channel). Owner-DM (``external_dm``) is a private 1:1 — the "公開場合,
不關妳的話" framing doesn't fit there, so it must NOT render. ``internal``
(the default) must not render it either.
"""
from __future__ import annotations

from dollos.mind.mind_state import MindState
from dollos.mind.mind_prompt import render_mind

# Distinctive substring from the scaffolding nudge — descriptive narration,
# not present in any other block.
_NUDGE_MARKER = "不回應是很正常的"


def test_external_public_turn_includes_scaffolding_nudge():
    out = render_mind(MindState(), [], "SYS", origin_tier="external_public")
    assert _NUDGE_MARKER in out


def test_internal_turn_does_not_include_scaffolding_nudge():
    out = render_mind(MindState(), [], "SYS")  # origin_tier defaults to "internal"
    assert _NUDGE_MARKER not in out


def test_external_dm_turn_does_not_include_scaffolding_nudge():
    """Owner DM is private 1:1 — the public-overhearing framing doesn't
    apply, so the nudge must not render here."""
    out = render_mind(MindState(), [], "SYS", origin_tier="external_dm")
    assert _NUDGE_MARKER not in out
