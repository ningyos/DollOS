"""P1c Task 6: L2 external-situation scaffolding nudge.

Once a turn is admitted past the code-side gate (Tasks 1-5, the MAIN
defense against 亂回), Doll can still choose Say or silence (the cascade
emits 0..N actions). This is the secondary, DESCRIPTIVE nudge — narration
of the situation, not a command — so silence reads as a normal option on
a public turn: she's overhearing a lot that isn't addressed to her.

It is part of the page snapshot (``render_epoch_start``), only on
``origin_tier == "external_public"`` (a stranger in a public channel).
Owner-DM (``external_dm``) is a private 1:1 — the "somewhere public,
hearing a lot that isn't said to you" framing doesn't fit there, so it must
NOT render. ``internal``
(the default) must not render it either.
"""
from __future__ import annotations

from dollos.mind.mind_state import MindState
from dollos.mind.mind_prompt import Seen, render_epoch_start

NOW = 1_790_000_000.0

# Distinctive substring from the scaffolding nudge — descriptive narration,
# not present in any other block.
_NUDGE_MARKER = "Not replying is normal here"


def test_external_public_turn_includes_scaffolding_nudge():
    out = render_epoch_start(MindState(), Seen(), "external_public", NOW, channel_id="c1")
    assert _NUDGE_MARKER in out
    assert (
        "[External situation]\n"
        "You're somewhere public and hear a lot that isn't said to you. Not replying "
        "is normal here; you speak up when you actually want to say something."
    ) in out


def test_internal_turn_does_not_include_scaffolding_nudge():
    out = render_epoch_start(MindState(), Seen(), "internal", NOW)
    assert _NUDGE_MARKER not in out


def test_external_dm_turn_does_not_include_scaffolding_nudge():
    """Owner DM is private 1:1 — the public-overhearing framing doesn't
    apply, so the nudge must not render here."""
    out = render_epoch_start(MindState(), Seen(), "external_dm", NOW, channel_id="dm1")
    assert _NUDGE_MARKER not in out
