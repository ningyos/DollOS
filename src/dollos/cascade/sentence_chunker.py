"""Sentence-boundary chunker for TTS-friendly streaming text.

Buffers tokens until a sentence-ending punctuation (or newline) is seen,
then emits the sentence as one chunk. Forced-flush after `max_chars` so
unpunctuated runs don't delay audio indefinitely.

Used by mind_loop between the parser's SpeakChunk events and the IPC
TextChunk emit so each TextChunk corresponds to a TTS-friendly unit.
"""
from __future__ import annotations

_PUNCT = {".", "?", "!", "。", "？", "！", "\n"}


class SentenceChunker:
    def __init__(self, max_chars: int = 120) -> None:
        self._buf = ""
        self._max = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        i = 0
        while i < len(self._buf):
            if self._buf[i] in _PUNCT:
                end = i + 1
                while end < len(self._buf) and self._buf[end] in (" ", "\n"):
                    end += 1
                sentence = self._buf[:end]
                self._buf = self._buf[end:]
                # A whitespace-only "sentence" (e.g. a lone "\n\n" preceding
                # the real reply) has no deliverable content — drop it rather
                # than yielding it as its own chunk (regression: this used to
                # surface as a standalone empty/whitespace outbound message).
                if sentence.strip():
                    out.append(sentence)
                i = 0
                continue
            i += 1
        while len(self._buf) >= self._max:
            out.append(self._buf[:self._max])
            self._buf = self._buf[self._max:]
        return out

    def flush(self) -> list[str]:
        if self._buf:
            tail = self._buf
            self._buf = ""
            if tail.strip():
                return [tail]
        return []
