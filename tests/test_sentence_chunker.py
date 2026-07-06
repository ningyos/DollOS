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
