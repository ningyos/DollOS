"""Task 1 (self-directed agenda): OpenLoop self-directed fields + AgendaMoment kind.

See docs/superpowers/sdd plan for this feature. Pure data-model extension —
no behavior change. Backward-compat with pre-existing persisted state
(open_loops JSON without the new fields) is the critical property here.
"""
import json
import typing
from dataclasses import asdict

from dollos.mind.mind_state import MindState, OpenLoop, load_state, save_state


def test_openloop_new_fields_default():
    ol = OpenLoop(id="x", desc="d", opened_at=1.0)
    assert ol.self_directed is False and ol.trigger == "" and ol.provenance == {} and ol.progress == []


def test_openloop_backward_compat_missing_fields(tmp_path):
    # old persisted state has open_loops without the new fields → _coerce fills defaults
    p = tmp_path / "mind_state.json"
    p.write_text(json.dumps({"open_loops": [{"id": "old", "desc": "d", "opened_at": 1.0}]}))
    st = load_state(p)
    assert st.open_loops[0].self_directed is False and st.open_loops[0].provenance == {}


def test_openloop_roundtrip_with_new_fields(tmp_path):
    st = MindState()
    st.open_loops.append(OpenLoop(id="g", desc="pursue", opened_at=2.0, self_directed=True,
                                  trigger="from chat", provenance={"turn_id": "5"}, progress=["step1"]))
    p = tmp_path / "s.json"
    save_state(st, p)
    back = load_state(p)
    assert back.open_loops[0].self_directed is True
    assert back.open_loops[0].provenance == {"turn_id": "5"} and back.open_loops[0].progress == ["step1"]


def test_agenda_moment_in_perception_kind_literal():
    import dollos.mind.mind_state as ms
    # `from __future__ import annotations` stores dataclass field .type as an
    # unresolved string; typing.get_type_hints() resolves the forward ref back
    # into the actual typing.Literal object so get_args() works.
    hints = typing.get_type_hints(ms.Perception)
    kinds = typing.get_args(hints["kind"])
    assert "AgendaMoment" in kinds
