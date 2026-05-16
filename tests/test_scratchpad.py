import pytest

from dollos.scratchpad import Scratchpad


def test_initial_state_empty() -> None:
    sp = Scratchpad()
    assert sp.read() == ""


def test_write_then_read() -> None:
    sp = Scratchpad()
    sp.write("hello")
    assert sp.read() == "hello"


def test_write_overwrites() -> None:
    sp = Scratchpad()
    sp.write("first")
    sp.write("second")
    assert sp.read() == "second"


def test_write_exceeds_cap_raises() -> None:
    sp = Scratchpad()
    with pytest.raises(ValueError, match="exceeds 2000 char cap"):
        sp.write("x" * 2001)


def test_write_at_exact_cap_succeeds() -> None:
    sp = Scratchpad()
    sp.write("x" * 2000)
    assert len(sp.read()) == 2000


def test_append_on_empty_does_not_prepend_newline() -> None:
    sp = Scratchpad()
    sp.append("first line")
    assert sp.read() == "first line"


def test_append_on_nonempty_prepends_newline() -> None:
    sp = Scratchpad()
    sp.write("line one")
    sp.append("line two")
    assert sp.read() == "line one\nline two"


def test_append_returns_new_total_length() -> None:
    sp = Scratchpad()
    sp.write("abc")
    new_total = sp.append("de")  # "abc\nde" = 6 chars
    assert new_total == 6


def test_append_exceeds_cap_raises() -> None:
    sp = Scratchpad()
    sp.write("x" * 1995)
    with pytest.raises(ValueError, match="exceed 2000 chars"):
        sp.append("yyyyy")  # 1995 + 1 (newline) + 5 = 2001


def test_edit_unique_match() -> None:
    sp = Scratchpad()
    sp.write("hello world")
    sp.edit("world", "there")
    assert sp.read() == "hello there"


def test_edit_no_match_raises() -> None:
    sp = Scratchpad()
    sp.write("hello world")
    with pytest.raises(ValueError, match="not found"):
        sp.edit("missing", "anything")


def test_edit_ambiguous_match_raises() -> None:
    sp = Scratchpad()
    sp.write("foo bar foo baz")
    with pytest.raises(ValueError, match="appears 2 times"):
        sp.edit("foo", "x")


def test_edit_overflow_raises() -> None:
    sp = Scratchpad()
    sp.write("x" * 1990 + "needle")  # 1996 chars
    with pytest.raises(ValueError, match="push scratchpad to"):
        sp.edit("needle", "z" * 100)  # would become 2090 chars


def test_clear_resets_to_empty() -> None:
    sp = Scratchpad()
    sp.write("something")
    sp.clear()
    assert sp.read() == ""


def test_clear_then_write_round_trip() -> None:
    sp = Scratchpad()
    sp.write("first")
    sp.clear()
    sp.write("second")
    assert sp.read() == "second"
