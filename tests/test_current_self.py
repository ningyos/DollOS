"""current_self pure module — artifact render + composition + tripwire (spec §3.1/§5)."""
from dollos.mind import current_self

# ---- render_section ----

def test_render_section_empty_when_none():
    assert current_self.render_section(None) == ""
    assert current_self.render_section("") == ""


def test_render_section_has_heading_and_descriptive_framing():
    out = current_self.render_section("我現在監控數字跳動時會主動來勁。")
    assert out.startswith("## Who I am now\n")
    # Her adopted text stays as she wrote it (Q7: Traditional Chinese).
    assert "我現在監控數字跳動時會主動來勁。" in out
    # Descriptive, not imperative; provenance-accurate (she adopted it).
    assert "adopted" in out
    assert "a description, not a command" in out
    # No imperative command phrasing.
    assert "you should" not in out.lower() and "you must" not in out.lower()


def test_render_section_frame_is_english():
    """The daemon's heading and framing carry no Chinese; only her text does."""
    import re
    out = current_self.render_section("X")
    assert not re.search(r"[　-〿㐀-鿿豈-﫿＀-￯]", out)


# ---- compose ----

def test_compose_empty_section_is_prefix_plus_suffix():
    prefix, suffix = "PREFIX\n", "\n# Behavior\n"
    assert current_self.compose(prefix, "", suffix) == prefix + suffix


def test_compose_places_section_between_prefix_and_suffix():
    prefix, suffix = "...## Taboos\n- no LARP\n", "\n# Behavior\nrules\n"
    section = current_self.render_section("我現在的樣子。")
    out = current_self.compose(prefix, section, suffix)
    assert out.index("no LARP") < out.index("## Who I am now") < out.index("# Behavior")


# ---- classify_tripwire ----

def test_tripwire_in_sync():
    assert current_self.classify_tripwire(
        file_text="X", sanctioned_text="X",
        adopt_old_text="W", last_edit_text=None) == "in_sync"


def test_tripwire_crash_repair():
    # File == old_text of latest adopt (the log-then-write window, spec §5).
    assert current_self.classify_tripwire(
        file_text="OLD", sanctioned_text="NEW",
        adopt_old_text="OLD", last_edit_text=None) == "crash_repair"


def test_tripwire_already_logged():
    assert current_self.classify_tripwire(
        file_text="HACK", sanctioned_text="X",
        adopt_old_text="W", last_edit_text="HACK") == "already_logged"


def test_tripwire_new_edit():
    assert current_self.classify_tripwire(
        file_text="HACK", sanctioned_text="X",
        adopt_old_text="W", last_edit_text=None) == "new_edit"


def test_tripwire_bootstrap_empty_file_in_sync():
    # No sanctioned predecessor, empty file → in sync (spec §5 bootstrap).
    assert current_self.classify_tripwire(
        file_text="", sanctioned_text=None,
        adopt_old_text=None, last_edit_text=None) == "in_sync"


def test_tripwire_bootstrap_nonempty_file_is_new_edit():
    assert current_self.classify_tripwire(
        file_text="somebody wrote this", sanctioned_text=None,
        adopt_old_text=None, last_edit_text=None) == "new_edit"


def test_tripwire_crash_repair_beats_new_edit_priority():
    # adopt_old_text match takes priority over a differing last_edit_text.
    assert current_self.classify_tripwire(
        file_text="OLD", sanctioned_text="NEW",
        adopt_old_text="OLD", last_edit_text="SOMETHING") == "crash_repair"


def test_tripwire_first_adoption_crash_window():
    # M3: adopt exists (sanctioned set), it was the first (old None), and the
    # file was never written (empty) → crash_repair, not a spurious new_edit.
    assert current_self.classify_tripwire(
        file_text="", sanctioned_text="新版的我",
        adopt_old_text=None, last_edit_text=None) == "crash_repair"


def test_tripwire_first_adoption_nonempty_file_is_new_edit():
    # Guard the boundary: with sanctioned set and old None but a NON-empty
    # divergent file, it's a genuine external edit — not the crash window.
    assert current_self.classify_tripwire(
        file_text="有人改的", sanctioned_text="新版的我",
        adopt_old_text=None, last_edit_text=None) == "new_edit"


def test_read_file_missing_is_empty(tmp_path):
    assert current_self.read_file(tmp_path / "current_self.md") == ""
