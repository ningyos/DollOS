"""Tests for build_voice_first_grammar's deliberate think line-length caps.

The voice_first grammar forces every turn to open with a 5-line
<think> block (SEEN/INTENT/TOOL/REVIEW/MOOD). Before this cap, `line`
had no upper bound (`line ::= [^\\n]+ "\\n"`), which let idle turns
free-run a 700+ token think tail. This binds ordinary think lines to
`_THINK_LINE_CAP` and gives REVIEW (persisted into recent_reviews) a
wider `_REVIEW_LINE_CAP` via its own `rline` rule. `speak` (spoken
output) is never bounded.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from dollos.llm.templates import (
    _REVIEW_LINE_CAP,
    _THINK_LINE_CAP,
    build_voice_first_grammar,
)


class _Dummy(BaseModel):
    text: str = Field(...)


def test_think_lines_are_length_bounded():
    g = build_voice_first_grammar([_Dummy])
    # 一般 think 行綁 _THINK_LINE_CAP
    assert f'line ::= [^\\n]{{1,{_THINK_LINE_CAP}}} "\\n"' in g
    # 舊的無上限規則不得殘留
    assert 'line ::= [^\\n]+ "\\n"' not in g


def test_review_uses_wider_rline_rule():
    g = build_voice_first_grammar([_Dummy])
    assert f'rline ::= [^\\n]{{1,{_REVIEW_LINE_CAP}}} "\\n"' in g
    # think 規則裡 REVIEW 用 rline、其餘用 line
    assert '"REVIEW: " rline "MOOD: " line' in g
    assert '"SEEN: " line "INTENT: " line "TOOL: " line' in g


def test_speak_stays_unbounded():
    g = build_voice_first_grammar([_Dummy])
    assert 'speak ::= [^<]+' in g  # 絕不綁口語長度


def test_caps_are_sane_defaults():
    assert _THINK_LINE_CAP == 64
    assert _REVIEW_LINE_CAP == 120
