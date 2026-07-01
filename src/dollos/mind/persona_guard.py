"""Mechanical persona-taboo enforcement + stability fingerprinting
(spec 2026-07-01 persona-hardening §2 and §3).

A weak/pressured local model can and does ignore prose-only constraints
(`character.py`'s `taboos` field has zero code-level consumer). This module
is the code-level consumer: a pure, no-I/O detector over a pack's declared
`Enforcement` rules. It does not block or rewrite anything — callers
(`mind_loop.py`) use it to *announce* violations, not silently censor them.

§3 adds the "IDENTITY_HASH" text-response-comparison building blocks used by
`scripts/persona_stability_smoke.py`: a normalized fingerprint of a response
(`fingerprint_response`), a cheap word-overlap drift signal against prior
runs (`response_drift_score`), and a JSONL baseline read/write pair
(`load_baselines` / `append_baseline`). All of these are pure or
filesystem-only (no network, no LLM) so they're unit-testable without a
live daemon — see `tests/test_persona_guard.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import string
import time
from pathlib import Path
from typing import TYPE_CHECKING

import jieba

if TYPE_CHECKING:
    from dollos.character import Enforcement

# jieba prints its dictionary-load line to logging at INFO; quiet it (same
# precedent as memory/fts_store.py, the other jieba consumer in this repo).
jieba.setLogLevel(logging.WARNING)

# ASCII "!" and fullwidth "！" are treated as the same exclaim character for
# run-length purposes — a mixed run like "!！" still counts as length 2.
_EXCLAIM_RUN_RE = re.compile(r"[!！]+")

# Common CJK punctuation/symbols not covered by `string.punctuation` (which
# is ASCII-only). Deliberately a flat literal set, not a Unicode-category
# scan — cheap and covers what actually shows up in Doll's speech.
_CJK_PUNCTUATION = "，。！？；：「」『』（）【】《》、～·…—“”‘’"
_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation + _CJK_PUNCTUATION)
_WHITESPACE_RE = re.compile(r"\s+")


def check_persona_violations(text: str, rules: "Enforcement") -> list[str]:
    """Return human-readable violation descriptions, or [] if clean.

    Checks each ``rules.banned_substrings`` entry via a case-sensitive
    literal ``in`` check, and — if ``rules.max_exclaim_run`` is set — flags
    any run of ``!``/``！`` longer than that threshold. Pure function, no I/O.
    """
    violations: list[str] = []

    for banned in rules.banned_substrings:
        if banned in text:
            violations.append(f"banned substring found: {banned!r}")

    if rules.max_exclaim_run is not None:
        for match in _EXCLAIM_RUN_RE.finditer(text):
            if len(match.group()) > rules.max_exclaim_run:
                violations.append(
                    f"exclaim run of {len(match.group())} exceeds max "
                    f"{rules.max_exclaim_run}: {match.group()!r}"
                )

    return violations


def _normalize_for_fingerprint(text: str) -> str:
    """Normalization shared by `fingerprint_response` and
    `response_drift_score`'s word-set comparison.

    Steps (in order): lowercase → strip leading/trailing whitespace →
    collapse internal whitespace runs to a single space → strip ASCII +
    common CJK punctuation → collapse whitespace again (punctuation
    removal can leave doubled spaces, e.g. "a, b" → "a  b") → strip.

    This is intentionally coarse: it's meant to make two responses that
    differ only in casing/spacing/punctuation fingerprint identically, not
    to be a linguistically rigorous tokenizer.
    """
    normalized = text.lower().strip()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = normalized.translate(_PUNCTUATION_TABLE)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def fingerprint_response(text: str) -> str:
    """Return a stable sha256 hex digest of `text`'s normalized form.

    Normalization: see `_normalize_for_fingerprint` (lowercase, strip,
    collapse whitespace, strip punctuation). Same input, or inputs that
    only differ in case/whitespace/punctuation, hash identically;
    genuinely different wording hashes differently. Pure function, no I/O.
    """
    normalized = _normalize_for_fingerprint(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _word_set(text: str) -> set[str]:
    """Tokenize normalized `text` into a set of words for Jaccard overlap.

    CJK is load-bearing here (Doll speaks 繁體中文) — plain `str.split()`
    on whitespace would treat an entire unsegmented Chinese sentence as one
    giant "word" (Chinese doesn't delimit words with spaces), making the
    overlap check meaningless for real responses. So this uses `jieba`
    (already a core dependency — same precedent as
    `memory/fts_store.py`'s FTS indexing) to segment CJK text into words;
    ASCII words split on whitespace as normal. Pure whitespace tokens are
    dropped.
    """
    normalized = _normalize_for_fingerprint(text)
    if not normalized:
        return set()
    return {tok for tok in jieba.cut(normalized) if tok.strip()}


def response_drift_score(new_text: str, baseline_texts: list[str]) -> float:
    """Jaccard overlap between `new_text`'s normalized word-set and the
    UNION of all `baseline_texts`' normalized word-sets (union, not
    max-per-baseline, is used so the score reflects overlap with anything
    Doll has said before across the whole history, not just the single
    closest-matching prior run).

    Returns a float in [0, 1]. 1.0 = identical vocabulary (no drift
    signal); 0.0 = completely disjoint word sets (maximally divergent).
    Lower means more divergent from history.

    If `baseline_texts` is empty (first run for a prompt — no history to
    compare against yet), returns 1.0: "no drift signal available" is
    treated the same as "no drift detected", not as "total drift" — the
    caller (the smoke script) has nothing to warn about on a first run.

    Pure function, no I/O, no embedding calls — a cheap tripwire, not a
    rigorous statistical drift test (see spec §3 / §6).
    """
    if not baseline_texts:
        return 1.0

    new_words = _word_set(new_text)
    baseline_words: set[str] = set()
    for baseline_text in baseline_texts:
        baseline_words |= _word_set(baseline_text)

    union = new_words | baseline_words
    if not union:
        return 1.0  # both sides normalize to nothing — trivially "identical"

    intersection = new_words & baseline_words
    return len(intersection) / len(union)


def load_baselines(path: Path) -> dict[str, list[str]]:
    """Read a JSONL baseline file into `{prompt_key: [response_text, ...]}`.

    Each line is one `{"prompt_key": ..., "response": ..., "fingerprint":
    ..., "ts": ...}` record (see `append_baseline`). A missing file returns
    `{}` rather than raising — this is a manually-invoked tool run by a
    human after pack/model changes, not a hot path, so "no baseline yet"
    (first run for a pack) must degrade gracefully.
    """
    if not path.exists():
        return {}

    baselines: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        baselines.setdefault(record["prompt_key"], []).append(record["response"])
    return baselines


def append_baseline(path: Path, prompt_key: str, response_text: str) -> None:
    """Append one run's response as a JSONL record to `path`.

    Creates parent directories (and the file itself) as needed. Each
    record carries the raw response, its `fingerprint_response` digest,
    and a unix timestamp, so the file doubles as both the drift-comparison
    corpus (`load_baselines`) and a plain audit log of what Doll actually
    said on each smoke run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "prompt_key": prompt_key,
        "response": response_text,
        "fingerprint": fingerprint_response(response_text),
        "ts": time.time(),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
