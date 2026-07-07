"""Tests for the [Your agenda] block — self-directed OpenLoops render
separately from [Open loops] (user-owed commitments)."""


def test_self_directed_renders_in_your_agenda_not_open_loops():
    from dollos.mind.mind_state import MindState, OpenLoop
    st = MindState()
    st.open_loops = [
        OpenLoop(id="a", desc="my curiosity", opened_at=1.0, self_directed=True,
                 trigger="chat about X", progress=["p1"]),
        OpenLoop(id="b", desc="owed TODO", opened_at=1.0, self_directed=False),
    ]
    from dollos.mind.mind_prompt import _render_your_agenda, _render_open_loops
    agenda = _render_your_agenda(st.open_loops, now=2.0)
    loops = _render_open_loops([l for l in st.open_loops if not l.self_directed], now=2.0)
    assert "my curiosity" in agenda and "chat about X" in agenda and "p1" in agenda
    assert "my curiosity" not in loops and "owed TODO" in loops
