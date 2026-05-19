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
