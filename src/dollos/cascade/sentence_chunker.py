"""Sentence-boundary chunker for TTS-friendly streaming text.

Buffers tokens until a sentence ends, then emits the sentence as one chunk.
Full-width 。？！ and newline end a sentence as soon as they appear. Half-width
.?! only end one when followed by whitespace, and a run of them ("...", "?!")
counts as a single terminator — so "3.14" and "Wait..." don't split mid-way.
Forced-flush after `max_chars` (cut after the last space, so English isn't
split mid-word) so unpunctuated runs don't delay audio indefinitely.

Used by mind_loop between the parser's SpeakChunk events and the IPC
TextChunk emit so each TextChunk corresponds to a TTS-friendly unit.
"""
from __future__ import annotations

_FULL_STOP = {"。", "？", "！", "\n"}
_HALF_STOP = {".", "?", "!"}
_SPACE = (" ", "\n")


class SentenceChunker:
    def __init__(self, max_chars: int = 120) -> None:
        self._buf = ""
        self._max = max_chars

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        i = 0
        while i < len(self._buf):
            ch = self._buf[i]
            if ch in _HALF_STOP:
                end = i + 1
                while end < len(self._buf) and self._buf[end] in _HALF_STOP:
                    end += 1
                # The run is the buffer's tail: whether it ends a sentence
                # depends on the next char, so wait for the next feed (the
                # reply's last sentence goes out via flush()).
                if end == len(self._buf):
                    break
                if self._buf[end] not in _SPACE:
                    i = end
                    continue
            elif ch in _FULL_STOP:
                end = i + 1
            else:
                i += 1
                continue
            while end < len(self._buf) and self._buf[end] in _SPACE:
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
        while len(self._buf) >= self._max:
            # Cut after the last space; one right past the limit still counts
            # (the chunk keeps it as its trailing char). No space at all →
            # hard cut at max_chars.
            cut = self._buf.rfind(" ", 0, self._max + 1) + 1 or self._max
            chunk = self._buf[:cut]
            self._buf = self._buf[cut:]
            if chunk.strip():
                out.append(chunk)
        return out

    def flush(self) -> list[str]:
        if self._buf:
            tail = self._buf
            self._buf = ""
            if tail.strip():
                return [tail]
        return []
