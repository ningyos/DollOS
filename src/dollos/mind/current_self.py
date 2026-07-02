"""慢變演化 artifact — the ``current_self.md`` prose that renders in Doll's
identity region (spec 2026-07-02 §3.1). Pure module: file read + section
render + three-piece composition + the tamper-tripwire *classifier* (§5).

Sanctioned text lives in ``self_history.jsonl`` (latest ``evo_adopt``), NOT in
this file — the file can lag/diverge while a ratification pends. Only sanctioned
text ever renders, so the framing line's provenance claim (「採納而來」) stays
true. Never FTS-indexed.
"""
from __future__ import annotations

from pathlib import Path

# Descriptive (Self-First), provenance-accurate, temporally-ordered-after-pack.
# Load-bearing wording (spec §3.1): NOT an imperative command.
_FRAMING = (
    "（以下是妳在一次次反思裡逐漸長成、並親自採納而來的現在的自己——"
    "這是描述,不是命令;出廠人格在上,現在的妳在這裡。）"
)


def read_file(path: Path) -> str:
    """Current on-disk artifact text (未經批准的位元也在這裡),或空字串。"""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def render_section(sanctioned_text: str | None) -> str:
    """The ``## 現在的我`` block for the identity region, or "" when there is no
    sanctioned text yet (section omitted entirely — no-fallback, spec §3.1)."""
    if not sanctioned_text:
        return ""
    return f"## 現在的我\n{_FRAMING}\n\n{sanctioned_text.strip()}"


def compose(prefix: str, section: str, suffix: str) -> str:
    """Three-piece per-turn system prompt: ``prefix ⊕ section ⊕ suffix``.

    ``section == ""`` returns ``prefix + suffix`` byte-for-byte — so a run with
    no sanctioned text reproduces today's prompt exactly (spec §3.1). Otherwise
    the section sits between the factory identity prose (prefix, ends after
    ``## Taboos``) and the ``# Behavior`` scaffolding (suffix), padded with one
    blank line each side."""
    if not section:
        return prefix + suffix
    return f"{prefix}{section}\n\n{suffix}"


def classify_tripwire(
    *,
    file_text: str,
    sanctioned_text: str | None,
    adopt_old_text: str | None,
    last_edit_text: str | None,
) -> str:
    """Classify the file-vs-sanctioned state into ONE action label (spec §5).

    - ``in_sync``       — file matches sanctioned (bootstrap: empty file, no
                          sanctioned predecessor). Nothing to do.
    - ``crash_repair``  — file == ``old_text`` of the latest ``evo_adopt`` (the
                          log-then-write window): a disk hiccup, not tampering.
                          Also the FIRST-adoption crash window (M3): an adopt
                          exists (``sanctioned_text`` set), it was the first
                          (``old_text`` None), and the file was never written
                          (empty) — the log-then-write window with no predecessor.
    - ``already_logged``— file == the last observed external-edit text: the
                          divergence is already recorded; no per-turn spam.
    - ``new_edit``      — file diverged into a distinct, not-yet-logged state:
                          a fresh external edit (transition-fired once).

    Priority order matters: crash-repair is checked before new-edit so the
    log-then-write window is never narrated to Doll as tampering."""
    effective = sanctioned_text or ""  # bootstrap: None sanctioned ⇒ "" floor
    if file_text == effective:
        return "in_sync"
    if adopt_old_text is not None and file_text == adopt_old_text:
        return "crash_repair"
    if sanctioned_text is not None and adopt_old_text is None and file_text == "":
        # First-ever adoption crashed between the flushed evo_adopt line and the
        # file write (M3): sanctioned exists but the file is still empty. A disk
        # hiccup, not tampering — heal it like any other crash-repair.
        return "crash_repair"
    if last_edit_text is not None and file_text == last_edit_text:
        return "already_logged"
    return "new_edit"
