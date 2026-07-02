"""self_history — append-only evidence log (spec 2026-07-02 slow-self-evolution §3.2)."""
import json

from dollos.mind import self_history


def test_log_event_appends_jsonl_with_ts(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=3, external_ctx=False,
                           section="self", id="s1", text="喜歡看監控數字跳動")
    self_history.log_event(p, kind="pin_remove", turn=5, external_ctx=False,
                           section="self", id="s1", text="", old_text="喜歡看監控數字跳動")
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in lines] == ["pin_add", "pin_remove"]
    assert lines[0]["turn"] == 3 and "ts" in lines[0]
    assert lines[1]["old_text"] == "喜歡看監控數字跳動"  # tombstone preserved


def test_log_event_creates_parent_dir(tmp_path):
    p = tmp_path / "deep" / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=1, external_ctx=True,
                           section="user", id="u1", text="主人熬夜")
    assert json.loads(p.read_text().splitlines()[0])["external_ctx"] is True


def test_last_pin_turn_finds_most_recent_matching(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=3, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="pin_reconfirm", turn=9, external_ctx=False,
                           section="self", id="s1", text="A")
    self_history.log_event(p, kind="pin_add", turn=11, external_ctx=False,
                           section="user", id="u1", text="A")  # other section, same text
    assert self_history.last_pin_turn(p, section="self", text="A") == 9
    assert self_history.last_pin_turn(p, section="user", text="A") == 11
    assert self_history.last_pin_turn(p, section="self", text="B") is None


def test_last_pin_turn_ignores_non_pin_kinds(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_remove", turn=4, external_ctx=False,
                           section="self", id="s1", text="", old_text="A")
    assert self_history.last_pin_turn(p, section="self", text="A") is None


def test_last_pin_turn_missing_file(tmp_path):
    assert self_history.last_pin_turn(tmp_path / "nope.jsonl", section="self", text="A") is None


def test_last_pin_turn_tolerates_torn_tail_line(tmp_path):
    p = tmp_path / "self_history.jsonl"
    self_history.log_event(p, kind="pin_add", turn=2, external_ctx=False,
                           section="self", id="s1", text="A")
    with p.open("a", encoding="utf-8") as f:
        f.write('{"kind": "pin_add", "turn"')  # torn write, no newline-terminated JSON
    assert self_history.last_pin_turn(p, section="self", text="A") == 2


def test_self_history_lives_outside_index_paths():
    """self_history.jsonl sits at memory_root root — FtsMemory only indexes
    [shared, transcripts, skills] subtrees, so it can never enter recall.
    Structural guard mirroring self_profile.md's (spec §3.2)."""
    from dollos.tools import PinSelf  # the only writer wires the path
    import inspect
    src = inspect.getsource(PinSelf.run)
    assert 'memory_root / "self_history.jsonl"' in src
    assert "index_file" not in src
