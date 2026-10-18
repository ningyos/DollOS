from dollos.cascade.sentence_chunker import SentenceChunker


def test_splits_on_english_punct():
    c = SentenceChunker()
    out = list(c.feed("Hello there. ")) + list(c.feed("How are you? ")) + list(c.feed("Fine"))
    out += list(c.flush())
    assert out == ["Hello there. ", "How are you? ", "Fine"]


def test_splits_on_chinese_punct():
    c = SentenceChunker()
    out = list(c.feed("你好。今天好嗎？")) + list(c.flush())
    assert out == ["你好。", "今天好嗎？"]


def test_splits_on_exclamation_and_newline():
    c = SentenceChunker()
    out = list(c.feed("Stop!\nWait")) + list(c.flush())
    assert out == ["Stop!\n", "Wait"]


def test_split_across_feeds():
    c = SentenceChunker()
    out = []
    out += list(c.feed("Hello th"))
    out += list(c.feed("ere. Bye"))
    out += list(c.flush())
    assert out == ["Hello there. ", "Bye"]


def test_forced_flush_on_max_chars():
    c = SentenceChunker(max_chars=10)
    out = list(c.feed("abcdefghijklmnop")) + list(c.flush())
    assert out[0] == "abcdefghij"
    assert "".join(out) == "abcdefghijklmnop"


def test_empty_input():
    c = SentenceChunker()
    assert list(c.feed("")) == []
    assert list(c.flush()) == []


def test_trailing_whitespace_after_punct_included():
    c = SentenceChunker()
    out = list(c.feed("Hi.  Bye")) + list(c.flush())
    assert out == ["Hi.  ", "Bye"]


def test_leading_newlines_before_reply_never_yield_whitespace_only_chunk_all_at_once():
    """Regression: an owner DM's raw output after `</think>` was
    `"\\n\\n主人好。"` (grammar boilerplate `\\n\\n` + reply). `SentenceChunker`
    treats `\\n` as a delimiter, so it used to split the leading `\\n\\n` into
    its OWN chunk and yield it FIRST — Discord rejects an empty/whitespace
    message with a 400, tearing down the whole bridge connection. Fed
    all-at-once, no chunk may be whitespace-only, and the real content must
    still come through intact."""
    c = SentenceChunker()
    out = list(c.feed("\n\n主人好。")) + list(c.flush())
    assert not any(not chunk.strip() for chunk in out), f"whitespace-only chunk leaked: {out!r}"
    assert "".join(out) == "主人好。"


def test_leading_newlines_before_reply_never_yield_whitespace_only_chunk_token_by_token():
    """Same regression as above, but fed one character at a time — mirrors
    real llama.cpp token-by-token streaming, where `</think>` and its
    trailing newlines can arrive as separate feed() calls."""
    c = SentenceChunker()
    out: list[str] = []
    for ch in "\n\n主人好。":
        out += list(c.feed(ch))
    out += list(c.flush())
    assert not any(not chunk.strip() for chunk in out), f"whitespace-only chunk leaked: {out!r}"
    assert "".join(out) == "主人好。"


def test_ellipsis_is_one_terminator():
    c = SentenceChunker()
    out = list(c.feed("Wait... what?")) + list(c.flush())
    assert out == ["Wait... ", "what?"]


def test_decimal_point_does_not_split():
    c = SentenceChunker()
    out = list(c.feed("Pi is 3.14 or so.")) + list(c.flush())
    assert out == ["Pi is 3.14 or so."]


def test_mixed_half_width_run_is_one_terminator():
    c = SentenceChunker()
    out = list(c.feed("Hmm?! Really.")) + list(c.flush())
    assert out == ["Hmm?! ", "Really."]


def test_half_width_punct_at_buffer_end_waits_for_next_feed():
    """The period is the last char of the first feed, so the chunker can't
    tell yet whether it ends a sentence ("Hello. ") or not ("3.14") — it
    holds it until the next feed shows the following char."""
    c = SentenceChunker()
    first = list(c.feed("Hello."))
    assert first == []
    out = first + list(c.feed(" Bye")) + list(c.flush())
    assert out == ["Hello. ", "Bye"]


def test_forced_flush_splits_after_last_space():
    c = SentenceChunker(max_chars=22)
    out = list(c.feed("the quick brown fox jumps over the lazy dog")) + list(c.flush())
    assert out == ["the quick brown fox ", "jumps over the lazy ", "dog"]


def test_forced_flush_space_right_at_limit_stays_with_chunk():
    """A space just past `max_chars` is still a word boundary — cutting there
    keeps "defghij" whole instead of splitting off a short "abc "."""
    c = SentenceChunker(max_chars=11)
    out = list(c.feed("abc defghij klm")) + list(c.flush())
    assert out == ["abc defghij ", "klm"]


def test_forced_flush_never_yields_whitespace_only_chunk():
    """The only space is the leading one, so cutting after it would yield a
    lone " " — dropped like any other whitespace-only chunk."""
    c = SentenceChunker(max_chars=5)
    out = list(c.feed(" abcdefgh")) + list(c.flush())
    assert out == ["abcde", "fgh"]
