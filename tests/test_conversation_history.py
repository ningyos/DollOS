from dollos.conversation_history import ConversationHistory


def test_initial_empty() -> None:
    h = ConversationHistory(max_turns=3)
    assert h.turn_count() == 0
    assert h.recent_messages() == []


def test_add_turn_then_recent_messages_round_trip() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    assert h.turn_count() == 1
    assert h.recent_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_multiple_turns_flatten_in_order() -> None:
    h = ConversationHistory(max_turns=10)
    h.add_turn([{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}])
    h.add_turn([{"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}])
    msgs = h.recent_messages()
    assert [m["content"] for m in msgs] == ["u1", "a1", "u2", "a2"]


def test_cap_drops_oldest() -> None:
    h = ConversationHistory(max_turns=2)
    h.add_turn([{"role": "user", "content": "u1"}])
    h.add_turn([{"role": "user", "content": "u2"}])
    h.add_turn([{"role": "user", "content": "u3"}])
    assert h.turn_count() == 2
    assert [m["content"] for m in h.recent_messages()] == ["u2", "u3"]


def test_empty_messages_ignored() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([])
    assert h.turn_count() == 0


def test_clear_resets() -> None:
    h = ConversationHistory(max_turns=3)
    h.add_turn([{"role": "user", "content": "x"}])
    h.clear()
    assert h.turn_count() == 0
    assert h.recent_messages() == []


def test_add_turn_defensive_copy() -> None:
    h = ConversationHistory(max_turns=3)
    msgs = [{"role": "user", "content": "original"}]
    h.add_turn(msgs)
    msgs.append({"role": "user", "content": "leak"})
    assert h.recent_messages() == [{"role": "user", "content": "original"}]
